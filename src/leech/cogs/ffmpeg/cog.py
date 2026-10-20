from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import discord
import httpx
import json

from leech.core.cog.cog import Cog
from leech.core.cog.context import CogContext
from leech.core.cog.error import CogError
from leech.core.command.mock.binding import MockBinding
from leech.core.command.native.binding import NativeBinding
from leech.core.command.runtime import Argument, Command
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.error import BaseError
from leech.core.helpers.attachments.attachments import gather_attachments

_logger = logging.getLogger(__name__)

CONVERT_NAME = "convert"
TRIM_NAME = "trim"

SERVER_URL = (
    os.getenv("LEECH_HUB_URL") or ""
).strip().rstrip("/")
SERVER_TOKEN = (
    os.getenv("LEECH_HUB_TOKEN")
    or ""
).strip()

AUDIO_FORMATS = ("mp3", "wav", "flac", "m4a", "opus", "copy")
VIDEO_FORMATS = ("mp4", "mkv", "webm", "gif")
FORMATS = AUDIO_FORMATS + VIDEO_FORMATS
RESOLUTIONS = (240, 360, 480, 720, 1080, 1440, 2160)
REPLY_LIMIT = 1990
POLL_SECS = 2.5
PROGRESS_MIN_SECS = 3.0
JOB_TIMEOUT_AUDIO = 600.0
JOB_TIMEOUT_VIDEO = 1800.0
URL_RE = re.compile(r"https?://[^\s)\"'<>\]]+")


class ServerError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


def _parse_time(text: str) -> Optional[float]:
    value = (text or "").strip()
    if not value:
        return None
    if ":" in value:
        parts = value.split(":")
        if len(parts) > 3 or not all(parts):
            return None
        try:
            if len(parts) == 3:
                hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
            else:
                hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
        except ValueError:
            return None
        if minutes >= 60 or seconds >= 60:
            return None
        return hours * 3600 + minutes * 60 + seconds
    if not re.match(r"^\d+(?:\.\d+)?$", value):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class _Args:
    url: Optional[str] = None
    format: Optional[str] = None
    bitrate: Optional[int] = None
    start: Optional[float] = None
    end: Optional[float] = None
    res: Optional[int] = None
    crf: Optional[int] = None
    fps: Optional[int] = None
    mute: bool = False
    unknown: list = field(default_factory=list)


def _parse_args(text: str, *, trim: bool) -> _Args:
    out = _Args()
    positional: list[str] = []
    for token in (text or "").split():
        if "=" in token and not token.lower().startswith("http"):
            key, _, value = token.partition("=")
            key = key.strip().lower()
            if key in ("url", "source") and value:
                out.url = value
            elif key in ("format", "fmt") and value:
                out.format = value.strip().lower()
            elif key in ("quality", "bitrate", "kbps") and value.strip().isdigit():
                out.bitrate = int(value.strip())
            elif key == "start":
                out.start = _parse_time(value)
            elif key == "end":
                out.end = _parse_time(value)
            elif key == "res" and value.strip().isdigit():
                out.res = int(value.strip())
            elif key == "crf" and value.strip().isdigit():
                out.crf = int(value.strip())
            elif key == "fps" and value.strip().isdigit():
                out.fps = int(value.strip())
            elif key == "mute":
                out.mute = _truthy(value)
            else:
                out.unknown.append(token)
        else:
            positional.append(token)

    if trim:
        times: list[str] = []
        rest: list[str] = []
        for token in positional:
            if len(times) < 2 and not token.lower().startswith("http") \
                    and _parse_time(token) is not None:
                times.append(token)
            else:
                rest.append(token)
        if len(times) >= 1:
            out.start = _parse_time(times[0])
        if len(times) >= 2:
            out.end = _parse_time(times[1])
        positional = rest

    for token in positional:
        low = token.lower()
        if low.startswith(("http://", "https://")) and out.url is None:
            out.url = token
        elif low in FORMATS and out.format is None:
            out.format = low
        elif low.isdigit() and 32 <= int(low) <= 320 and out.bitrate is None:
            out.bitrate = int(low)
        else:
            out.unknown.append(token)
    return out


def _validate_opts(args: _Args) -> None:
    if args.res is not None and args.res not in RESOLUTIONS:
        raise CogError(
            f"res must be one of {', '.join(map(str, RESOLUTIONS))} — "
            "like `res=720`.", is_user_error=True,
        )
    if args.crf is not None and not (14 <= args.crf <= 34):
        raise CogError("crf must be 14-34 (lower = better quality).",
                       is_user_error=True)
    if args.fps is not None and not (1 <= args.fps <= 120):
        raise CogError("fps must be 1-120.", is_user_error=True)
    videoish = args.format in VIDEO_FORMATS
    if (args.res or args.crf or args.fps or args.mute) and not videoish:
        raise CogError(
            "res/crf/fps/mute are video options. pick mp4, mkv, webm or gif.",
            is_user_error=True,
        )


def _fmt_pos(seconds: float) -> str:
    value = max(0.0, float(seconds))
    hours, rem = divmod(int(value), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{int(round((value % 1) * 100)):02d}"
    return f"{minutes}:{secs:02d}.{int(round((value % 1) * 100)):02d}"


class _Server:
    def __init__(self):
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            if method == "GET":
                resp = await self._http.get(
                    SERVER_URL + path, headers={"x-leech-token": SERVER_TOKEN})
            else:
                resp = await self._http.post(
                    SERVER_URL + path, json=body,
                    headers={"x-leech-token": SERVER_TOKEN})
        except httpx.HTTPError as exc:
            raise ServerError(
                f"leech-hub unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 401:
            raise ServerError(
                "the leech-hub rejected the token "
                "(LEECH_HUB_TOKEN vs the hub's TOKEN)."
            )
        if resp.status_code != 200:
            try:
                detail = str(resp.json()["error"])
            except Exception:
                try:
                    detail = str(resp.json()["detail"])
                except Exception:
                    detail = resp.text[:200]
            raise ServerError(f"leech hub answered {resp.status_code}: {detail}")        
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise ServerError(f"the leech-hub returned invalid json, details: {e}") from e
        if not data.get("ok"):
            raise ServerError(str(data.get("error") or "unknown hub error"))
        return data

    async def submit_convert(self, args: _Args, url: str) -> str:
        data = await self._api("POST", "/api/ffmpeg/convert", body={
            "url": url, "format": args.format or "mp3",
            "bitrate": args.bitrate, "res": args.res, "crf": args.crf,
            "fps": args.fps, "mute": args.mute,
        })
        return str(data["job"])

    async def submit_trim(self, args: _Args, url: str) -> str:
        data = await self._api("POST", "/api/ffmpeg/trim", body={
            "url": url, "format": args.format, "bitrate": args.bitrate,
            "res": args.res, "crf": args.crf, "fps": args.fps, "mute": args.mute,
            "start": _fmt_pos(args.start), "end": _fmt_pos(args.end),
        })
        return str(data["job"])

    async def job_status(self, job_id: str) -> dict:
        data = await self._api("GET", f"/api/jobs/{job_id}")
        return dict(data.get("job") or {})


class FfmpegCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        if not SERVER_URL or not SERVER_TOKEN:
            _logger.warning(
                "ffmpeg cog: no leech hub (LEECH_HUB_URL / LEECH_HUB_TOKEN) "
                ". not loading"
            )
            return
        self.context.add_on_load(self.load)
        self.context.add_on_unload(self.shutdown)
        self.server = _Server()

    async def shutdown(self) -> None:
        if hasattr(self, "server"):
            await self.server.aclose()

    async def load(self):
        convert_cmd = Command(
            name=CONVERT_NAME,
            description="convert an audio or video file (attached or replied to or linked).",
            usage_example=">convert mp3 320",
            help_content=(
                "the file comes from an attachment or the message you reply "
                "to or a url in the command. audio formats: mp3 (default), "
                "wav, flac, m4a, opus, copy (audio remux) + optional kbps "
                "32-320. video formats: mp4, mkv, webm, gif + optional "
                "res=/crf=/fps=/mute= (e.g. `>convert clip.mkv mp4 res=720`). "
                "converting runs on the leech-hub, you get a cdn download link back."
            ),
            arguments=[Argument(
                name="what",
                description="[url] [format] [kbps] [opts], e.g. 'mp3 320' or "
                            "'https://... clip.mkv webm res=720'.",
                usage_example="what=mp3 320",
                type=TypesRegistry.require(Types.STR),
                required=True,
            )],
            subcommands={},
            binding=NativeBinding(handler=self.convert_handler),
        )
        trim_cmd = Command(
            name=TRIM_NAME,
            description="cut a slice out of an audio or video file.",
            usage_example=">trim 1:30 2:05",
            help_content=(
                "times are seconds or m:ss or h:mm:ss. without a format "
                "it's a stream copy. instant and lossless but the cut "
                "points snap a bit early/late. name a format (any convert "
                "format + opts) to reencode for exact cuts. source: "
                "attachment, the message you reply to, or a url."
            ),
            arguments=[Argument(
                name="what",
                description="<start> <end> [url] [format] [opts].",
                usage_example="what=1:30 2:05 mp3",
                type=TypesRegistry.require(Types.STR),
                required=True,
            )],
            subcommands={},
            binding=NativeBinding(handler=self.trim_handler),
        )
        for command in (convert_cmd, trim_cmd):
            if not self.context.add_command(command.name, command):
                raise CogError(f"i failed to load '{command.name}' command.")

    async def _reply(self, message: discord.Message, text: str) -> discord.Message:
        return await message.reply(
            text[:REPLY_LIMIT],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _resolve_source(self, invocation: Invocation) -> Optional[str]:
        message = invocation.message
        try:
            attachments = await gather_attachments(message)
        except Exception:
            attachments = []
        if attachments:
            return getattr(attachments[0], "url", None)

        resolved = None
        ref = message.reference
        if ref is not None:
            resolved = ref.cached_message or getattr(ref, "resolved", None)
            if resolved is None and ref.message_id is not None:
                try:
                    resolved = await message.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    resolved = None
        if isinstance(resolved, discord.Message):
            try:
                replied_attachments = await gather_attachments(resolved)
            except Exception:
                replied_attachments = []
            if replied_attachments:
                return getattr(replied_attachments[0], "url", None)
            for url in URL_RE.findall(resolved.content or ""):
                return url
        return None

    def _progress_text(self, note: str, job: dict) -> str:
        progress = job.get("progress") or {}
        stage = str(progress.get("stage") or "working")
        text = f"{note} - {stage}"
        if progress.get("note"):
            text += f": {progress['note']}"
        return text[:REPLY_LIMIT]

    async def _run_job(self, message: discord.Message, job_id: str, note: str,
                       timeout: float) -> tuple[dict, discord.Message]:
        status_msg = await self._reply(message, f"{note} - queued...")
        last_stage = ""
        last_edit = 0.0
        deadline = time.monotonic() + timeout
        while True:
            job = await self.server.job_status(job_id)
            state = str(job.get("status") or "")
            if state == "done":
                return dict(job.get("result") or {}), status_msg
            if state == "error":
                raise ServerError(str(job.get("error") or "the job failed on the runner."))
            stage = self._progress_text(note, job)
            now = time.monotonic()
            if stage != last_stage and now - last_edit >= PROGRESS_MIN_SECS:
                try:
                    await status_msg.edit(content=stage)
                except discord.HTTPException:
                    pass
                last_stage, last_edit = stage, now
            if now >= deadline:
                raise ServerError(
                    "gave up waiting for the runner (timeout). (it may still "
                    "finish and the link would be lost), you should try again 😁"
                )
            await asyncio.sleep(POLL_SECS)

    async def _finish(self, status_msg: discord.Message, text: str) -> None:
        try:
            await status_msg.edit(content=text[:REPLY_LIMIT])
        except discord.HTTPException:
            await self._reply(status_msg, text)

    def _final(self, result: dict) -> str:
        name = result.get("filename") or "output"
        fmt = str(result.get("format") or "?")
        if fmt == "copy":
            label = "stream copy (lossless)"
        elif fmt in VIDEO_FORMATS:
            label = fmt
        else:
            label = fmt
            bitrate = result.get("bitrate")
            if bitrate and fmt in ("mp3", "m4a", "opus"):
                label += f" {bitrate}"
        head = f"**{name}** · {label}"
        if result.get("duration"):
            try:
                seconds = int(float(result["duration"]))
                head += f" · {seconds // 60}:{seconds % 60:02d}"
            except (TypeError, ValueError):
                pass
        if result.get("size"):
            head += f" · {result['size'] / 1e6:.1f} mb"
        lines = [head]
        url = result.get("url")
        if not url and result.get("path"):
            url = f"{SERVER_URL}{result['path']}"
        if url:
            lines.append(f"[download]({url})")
        return "\n".join(lines)


    async def convert_handler(self, invocation: Invocation):
        args = _parse_args(invocation.arguments["what"], trim=False)
        if args.unknown:
            raise CogError(
                f"didn't get {', '.join(args.unknown)} - usage: "
                f"`>{CONVERT_NAME} [url] [format] [kbps] [opts]` "
                f"(formats: {', '.join(FORMATS)}) or attach / reply to a file.",
                is_user_error=True,
            )
        _validate_opts(args)
        url = args.url or await self._resolve_source(invocation)
        if not url:
            raise CogError(
                "nothing to convert - attach a file, reply to one, or put a "
                f"url in the command. usage: `>{CONVERT_NAME} mp3 320`.",
                is_user_error=True,
            )
        fmt = args.format or "mp3"
        video = fmt in VIDEO_FORMATS
        label = fmt
        if args.bitrate and fmt in ("mp3", "m4a", "opus"):
            label += f" {args.bitrate}"
        if video and args.res:
            label += f" {args.res}p"
        job_id = await self.server.submit_convert(args, url)
        timeout = JOB_TIMEOUT_VIDEO if video else JOB_TIMEOUT_AUDIO
        result, status_msg = await self._run_job(
            invocation.message, job_id, f"converting to {label}", timeout)
        await self._finish(status_msg, self._final(result))

    async def trim_handler(self, invocation: Invocation):
        args = _parse_args(invocation.arguments["what"], trim=True)
        if args.start is None or args.end is None or args.end <= args.start:
            raise CogError(
                "i need two times first. seconds or m:ss or h:mm:ss - usage: "
                f"`>{TRIM_NAME} 1:30 2:05 [url] [format] [opts]`.",
                is_user_error=True,
            )
        if args.unknown:
            raise CogError(
                f"didn't get {', '.join(args.unknown)} - usage: "
                f"`>{TRIM_NAME} <start> <end> [url] [format] [opts]`.",
                is_user_error=True,
            )
        _validate_opts(args)
        url = args.url or await self._resolve_source(invocation)
        if not url:
            raise CogError(
                "nothing to trim - attach a file or reply to one or put a url "
                f"in the command. usage: `>{TRIM_NAME} 0:10 0:25`.",
                is_user_error=True,
            )
        fmt = args.format or "copy"
        note = (
            f"trimming {_fmt_pos(args.start)}-{_fmt_pos(args.end)}"
            + ("" if fmt == "copy" else f" -> {fmt}")
        )
        job_id = await self.server.submit_trim(args, url)
        timeout = (JOB_TIMEOUT_VIDEO if fmt in VIDEO_FORMATS
                   else JOB_TIMEOUT_AUDIO)
        result, status_msg = await self._run_job(invocation.message, job_id, note,
                                                 timeout)
        await self._finish(status_msg, self._final(result))


def create_cog(context: CogContext):
    return FfmpegCog(context=context)
