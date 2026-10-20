from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Optional

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

_logger = logging.getLogger(__name__)

SERVER_URL = (
    os.getenv("LEECH_HUB_URL") or ""
).strip().rstrip("/")
SERVER_TOKEN = (
    os.getenv("LEECH_HUB_TOKEN")
    or ""
).strip()
TIMEOUT = float(os.getenv("LEECH_YT_TIMEOUT", "1800") or 1800)
MAX_MB = float(os.getenv("LEECH_YT_MAX_MB", "200") or 200)
POLL_SECS = 2.5
PROGRESS_MIN_SECS = 3.0
REPLY_LIMIT = 1990

_URL_RE = re.compile(r"^https?://\S+$")


class HubError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


def _parse_seconds(raw: str) -> float:
    parts = raw.strip().split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        raise CogError(f"`{raw}` isn't a time. use seconds (67) or mm:ss (1:67).",
                       is_user_error=True)
    if not numbers or len(numbers) > 3 or any(n < 0 for n in numbers):
        raise CogError(f"`{raw}` isn't a time. use seconds (67) or mm:ss (1:67).",
                       is_user_error=True)
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


class _Hub:
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
            raise HubError(
                f"leech-hub unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 401:
            raise HubError(
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
            raise HubError(f"leech-hub answered {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise HubError(
                f"the leech-hub returned invalid json, details: {e}"
            ) from e
        if not data.get("ok"):
            raise HubError(str(data.get("error") or "unknown hub error"))
        return data

    async def submit_yt(self, payload: dict) -> str:
        data = await self._api("POST", "/api/jobs",
                               body={"kind": "yt", "payload": payload})
        return str(data["job"])

    async def job_status(self, job_id: str) -> dict:
        data = await self._api("GET", f"/api/jobs/{job_id}")
        return dict(data.get("job") or {})

    async def health(self) -> dict:
        return await self._api("GET", "/api/health")


class YtdlpCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        if not SERVER_URL or not SERVER_TOKEN:
            _logger.warning(
                "ytdlp cog: no leech hub (LEECH_HUB_URL / LEECH_HUB_TOKEN) "
                ". not loading"
            )
            return
        self.context.add_on_load(self.load)
        self.context.add_on_unload(self.shutdown)
        self.hub = _Hub()

    async def shutdown(self) -> None:
        if hasattr(self, "hub"):
            await self.hub.aclose()

    async def load(self):
        root = Command(
            name="ytdlp",
            description="fetch a video/audio link.",
            usage_example=">ytdlp https://youtu.be/YUqCwBa2iIk",
            help_content=(
                "downloads + converts it on the leech-hub and the finished "
                "file lands on the store chain (pixeldrain...) - you "
                "get a direct cdn link, big files included. video defaults to "
                "720p mp4. audio=on gets mp3 only. quality= caps video height "
                "(144-2160). max_mb= is the size cap (default 200). "
                "start=/end= in seconds or mm:ss cuts a clip. no runner "
                "awake? the job waits in the queue. `>ytdlp queue` shows it."
            ),
            arguments=[
                Argument(
                    name="url",
                    description="the media link.",
                    usage_example="https://youtu.be/pqMrFVuF-nc",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                ),
                Argument(
                    name="audio",
                    description="on = mp3 instead of video.",
                    usage_example="audio=on",
                    type=TypesRegistry.require(Types.BOOL),
                    required=False,
                ),
                Argument(
                    name="quality",
                    description="max video height (default 720).",
                    usage_example="quality=1080",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
                Argument(
                    name="max_mb",
                    description="size cap (default 200).",
                    usage_example="max_mb=50",
                    type=TypesRegistry.require(Types.INT),
                    required=False,
                ),
                Argument(
                    name="start",
                    description="clip start (seconds or mm:ss).",
                    usage_example="start=1:67",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
                Argument(
                    name="end",
                    description="clip end (seconds or mm:ss).",
                    usage_example="end=1:67",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
            ],
            subcommands={
                "queue": Command(
                    name="queue",
                    description="hub queue + runner status.",
                    usage_example=">ytdlp queue",
                    help_content=(
                        "asks the hub: how many jobs are queued and is the "
                        "runner dispatch armed. if dispatch is off, the "
                        "janitor still sweeps every 15 minutes."
                    ),
                    arguments=[],
                    subcommands={},
                    binding=NativeBinding(handler=self.queue_handler),
                ),
            },
            binding=NativeBinding(handler=self.yt_handler),
        )
        if not self.context.add_command(root.name, root):
            raise CogError(f"i failed to load '{root.name}' ytdlp command.")


    async def _reply(self, message: discord.Message, text: str) -> discord.Message:
        return await message.reply(
            text[:REPLY_LIMIT],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _progress_text(self, note: str, job: dict) -> str:
        progress = job.get("progress") or {}
        stage = str(progress.get("stage") or "working")
        total = progress.get("total")
        text = note
        if isinstance(total, int) and total > 0:
            text += f" - {stage} {progress.get('done', 0)}/{total}"
        else:
            text += f" - {stage}"
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
            job = await self.hub.job_status(job_id)
            state = str(job.get("status") or "")
            if state == "done":
                return dict(job.get("result") or {}), status_msg
            if state == "error":
                raise HubError(
                    str(job.get("error") or "the job failed on the runner."))
            stage = self._progress_text(note, job)
            now = time.monotonic()
            if stage != last_stage and now - last_edit >= PROGRESS_MIN_SECS:
                try:
                    await status_msg.edit(content=stage)
                except discord.HTTPException:
                    pass
                last_stage, last_edit = stage, now
            if now >= deadline:
                raise HubError(
                    "gave up waiting for the runner (timeout). (it may still "
                    "finish and the link would be lost), you should try again 😁"
                )
            await asyncio.sleep(POLL_SECS)


    async def yt_handler(self, invocation: Invocation):
        url = str(invocation.arguments["url"]).strip()
        if not _URL_RE.match(url):
            raise CogError("invalid link 😁", is_user_error=True)

        payload: dict[str, Any] = {"url": url}
        audio = invocation.arguments.get("audio") or None
        if audio:
            payload["audio"] = True
        quality = invocation.arguments.get("quality") or None
        if quality:
            try:
                quality = int(str(quality))
            except ValueError:
                raise CogError("quality must be a number like 720.",
                               is_user_error=True)
            if not 144 <= quality <= 2160:
                raise CogError(
                    "quality must be between 144 and 2160 pixels.",
                    is_user_error=True)
            payload["quality"] = quality
        max_mb = invocation.arguments.get("max_mb") or None
        if max_mb:
            try:
                max_mb = float(max_mb)
            except (TypeError, ValueError):
                raise CogError("max_mb must be a number like 50.",
                               is_user_error=True)
            if max_mb < 1:
                raise CogError("max_mb must be at least 1mb.",
                               is_user_error=True)
            payload["max_mb"] = min(max_mb, MAX_MB)
        for name in ("start", "end"):
            raw = str(invocation.arguments.get(name) or "").strip()
            if raw:
                payload[name] = _parse_seconds(raw)
        if "start" in payload and "end" in payload \
                and payload["end"] <= payload["start"]:
            raise CogError(
                "invalid clip 'end' has to be after 'start'.",
                is_user_error=True)

        note = "yt: audio (mp3)" if payload.get("audio") else \
               f"yt: video ({payload.get('quality') or 720}p)"
        job_id = await self.hub.submit_yt(payload)
        result, status_msg = await self._run_job(
            invocation.message, job_id, note, TIMEOUT)
        await self._deliver(invocation, status_msg, result)

    async def queue_handler(self, invocation: Invocation):
        try:
            health = await self.hub.health()
        except HubError as e:
            await invocation.message.reply(
                f"hub unreachable 😟: {e.message}", mention_author=False)
            return
        runner = health.get("runner") or {}
        if runner.get("dispatch"):
            runner_text = f"armed ({runner.get('repo') or '?'})"
        else:
            runner_text = ("dispatch off - janitor sweeps every 15 min "
                           "(jobs still queue)")
        await self._reply(
            invocation.message,
            f"```\nqueued: {health.get('queue_depth', 0)}\n"
            f"runner: {runner_text}\n"
            f"kinds: {', '.join(health.get('kinds') or [])}\n```",
        )

    async def _deliver(self, invocation: Invocation, status: discord.Message,
                       result: dict):
        url = str(result.get("url") or "")
        if not url:
            await self._fail(status, "the runner finished but sent no link 😟")
            return
        size = result.get("size_mb")
        if size is None and result.get("size"):
            try:
                size = round(float(result["size"]) / 1e6, 1)
            except (TypeError, ValueError):
                size = None
        embed = discord.Embed(
            title=str(result.get("title") or "download")[:250],
            url=str(result.get("webpage_url") or url),
            color=0x5865F2,
        )
        if result.get("uploader"):
            embed.set_author(name=str(result["uploader"])[:100])
        fields = [
            ("file", f"{size if size is not None else '?'}mb · "
                     f"{result.get('format') or ('mp3' if result.get('audio') else 'mp4')}"),
            ("duration", self._fmt_duration(result.get("duration"))),
        ]
        for name, value in fields:
            if value and value != "?":
                embed.add_field(name=name, value=value, inline=True)
        embed.set_footer(text="leech · hub ytdlp")
        thumb = str(result.get("thumbnail") or "")
        if thumb.startswith("https"):
            embed.set_thumbnail(url=thumb)
        try:
            await status.delete()
        except (discord.HTTPException, TypeError):
            pass
        await invocation.message.reply(content=url, embed=embed,
                                       mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _fmt_duration(value) -> str:
        try:
            seconds = int(float(value or 0))
        except (TypeError, ValueError):
            return "?"
        if seconds <= 0:
            return "?"
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

    async def _fail(self, status: discord.Message, message: str):
        try:
            await status.channel.send(f"yt failed 😟: {message}")
        except discord.HTTPException:
            _logger.warning("yt failed (status gone) 😟: %s", message)
        try:
            await status.delete()
        except (discord.HTTPException, TypeError):
            pass


def create_cog(context: CogContext):
    return YtdlpCog(context=context)
