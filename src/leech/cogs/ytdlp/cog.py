from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import discord
import httpx

from leech.core.cog.cog import Cog, CogContext
from leech.core.cog.error import CogError
from leech.core.command.runtime import Command, Argument
from leech.core.command.native.binding import NativeBinding
from leech.core.command.mock.binding import MockBinding
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry

_logger = logging.getLogger(__name__)

MANAGER = (os.getenv("LEECH_YT_URL") or os.getenv("LEECH_TTS_URL") or "").strip().rstrip("/")
TOKEN = (os.getenv("LEECH_YT_TOKEN") or os.getenv("LEECH_TTS_TOKEN") or "").strip()
TIMEOUT = float(os.getenv("LEECH_YT_TIMEOUT", "1800") or 1800)
MAX_MB = float(os.getenv("LEECH_YT_MAX_MB", "200") or 200)
POLL_SECS = 4.0
MIN_UPDATE_SECS = 8.0 

_URL_RE = re.compile(r"^https?://\S+$")
_TRUTHY = {"1", "true", "yes", "on"}


class _ManagerError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # offline | auth | bad | server
        self.message = message


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


class YtdlpCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)

    async def load(self):
        for name, command in {
            **self.get_ytdlp(),
        }.items():
            if not self.context.add_command(name, command):
                raise CogError(f"i failed to load '{name}' ytdlp command.")
        if not MANAGER:
            _logger.warning("ytdlp: LEECH_YT_URL/LEECH_TTS_URL not set. `>ytdlp` won't work.")

    def get_ytdlp(self) -> dict[str, Command]:
        return {
            "ytdlp": Command(
                name="ytdlp",
                description="fetch a video/audio link.",
                usage_example=">ytdlp https://youtu.be/YUqCwBa2iIk",
                help_content=(
                    "downloads + converts it and the finished file lands on "
                    "a free file host (x0.at or similar - the worker tries "
                    "several). you get a direct link, big files included (up "
                    "to 200mb). video defaults to 720p mp4. audio=1 gets mp3. "
                    "start=/end= creates a clip (seconds or mm:ss). if no worker "
                    "session is alive the job just waits in the queue until a valid worker picks it. `>ytdlp queue`"
                    "shows if there is anything in queue."
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
                        description="size cap before reencoding (default 200).",
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
                subcommands={**self.get_ytdlp_queue()},
                binding=NativeBinding(handler=self.yt_handler),
            )
        }

    def get_ytdlp_queue(self) -> dict[str, Command]:
        return {
            "queue": Command(
                name="queue",
                description="cloud queue + worker status.",
                usage_example=">ytdlp queue",
                help_content=(
                    "asks the manager: how many jobs queued, is a worker "
                    "session alive, which engine it runs. if no worker is "
                    "listed, activate cloud first."
                ),
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.ytq_handler),
            )
        }

    async def yt_handler(self, invocation: Invocation):
        self._require_manager()
        url = str(invocation.arguments["url"]).strip()
        if not _URL_RE.match(url):
            raise CogError("invalid youtube link 😁", is_user_error=True)

        payload: dict[str, Any] = {"url": url}
        audio = invocation.arguments.get("audio") or None
        if audio:
            payload["audio"] = audio
        quality = invocation.arguments.get("quality") or None
        if quality:
            try:
                quality = int(str(quality))
            except ValueError:
                raise CogError("quality must be a number like 720.",
                               is_user_error=True)
            if not 144 <= quality <= 2160:
                raise CogError("quality must be between 144 and 2160 pixels.", is_user_error=True)
            payload["quality"] = quality
        max_mb = invocation.arguments.get("max_mb") or None
        if max_mb:
            payload["max_mb"] = min(max_mb, MAX_MB)
            if payload["max_mb"] < 1:
                raise CogError("max_mb must be at least 1mb.", is_user_error=True)
        for name in ("start", "end"):
            raw = str(invocation.arguments.get(name) or "").strip()
            if raw:
                payload[name] = _parse_seconds(raw)
        if "start" in payload and "end" in payload \
                and payload["end"] <= payload["start"]:
            raise CogError("invalid clip 'end' has to be after 'start'.", is_user_error=True)

        job = await self._enqueue("yt", payload)
        status = await invocation.message.reply(
            f"yt: queued (position {job['position']})...", mention_author=False
        )
        result = await self._await_job(status, job["id"])
        if result is None:
            return
        await self._deliver(invocation, status, result)

    async def ytq_handler(self, invocation: Invocation):
        self._require_manager()
        try:
            data = await self._status_page()
        except _ManagerError as e:
            await invocation.message.reply(f"manager unreachable 😟: {e.message}",
                                           mention_author=False)
            return
        workers = data.get("workers") or []
        if workers:
            lines = [
                f"worker `{w.get('id')}` - {w.get('engine') or '?'}"
                + (f", {w['gpu']}" if w.get("gpu") else "")
                + f", seen {w.get('last_seen_ago')}s ago"
                for w in workers
            ]
            worker_text = "\n".join(lines)
        else:
            worker_text = "no worker session is alive. jobs are waiting. " \
                          " activate cloud first."
        await invocation.message.reply(
            f"```\nqueued: {data.get('queued')}\ninflight: {data.get('inflight')}"
            f"\nworkers:\n{worker_text}\n```",
        )

    def _require_manager(self):
        if not MANAGER:
            raise CogError(
                "no manager url set LEECH_YT_URL (or LEECH_TTS_URL). fix leech.",
                is_user_error=False,
            )

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if TOKEN:
            headers["x-leech-token"] = TOKEN
        return headers

    async def _call(self, method: str, path: str, json_body: dict | None = None) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method == "GET":
                    return await client.get(MANAGER + path, headers=self._headers())
                return await client.post(
                    MANAGER + path, json=json_body, headers=self._headers()
                )
        except httpx.HTTPError as e:
            raise _ManagerError("offline", f"can't reach the manager 😟 ({e.__class__.__name__})")

    async def _enqueue(self, kind: str, payload: dict) -> dict:
        response = await self._call("POST", "/api/jobs", {"kind": kind, "payload": payload})
        if response.status_code == 401:
            raise _ManagerError("auth", "token rejected. LEECH_YT_TOKEN vs manager TOKEN. fix leech.")
        if response.status_code == 400:
            detail = "?"
            try:
                detail = response.json().get("detail", "?")
            except Exception:  # noqa: BLE001
                pass
            raise CogError(str(detail), is_user_error=True)
        if response.status_code >= 400:
            raise _ManagerError("server", f"manager said {response.status_code}")
        return response.json()

    async def _job_view(self, job_id: str) -> dict:
        response = await self._call("GET", f"/api/jobs/{job_id}")
        if response.status_code == 404:
            raise _ManagerError("server", "job gone maybe expired?")
        if response.status_code >= 400:
            raise _ManagerError("server", f"manager said {response.status_code}")
        return response.json()

    async def _status_page(self) -> dict:
        response = await self._call("GET", "/api/status")
        if response.status_code == 401:
            raise _ManagerError("auth", "token rejected. LEECH_YT_TOKEN vs manager TOKEN. fix leech.")
        if response.status_code >= 400:
            raise _ManagerError("server", f"manager said {response.status_code}")
        return response.json()


    async def _await_job(self, status: discord.Message, job_id: str) -> dict | None:
        shown = "queued"
        last_post = time.monotonic()
        waited = 0.0
        while True:
            try:
                view = await self._job_view(job_id)
            except _ManagerError as e:
                await self._fail(status, e.message)
                return None
            state = view.get("status")
            if state == "done":
                result = view.get("result") or {}
                if not result:
                    await self._fail(status, "worker returned an empty result 😟")
                    return None
                return result
            if state == "error":
                await self._fail(status, str(view.get("error") or "?"))
                return None

            label = state or "queued"
            if state == "queued":
                position = view.get("position")
                label = f"queued (position {position})" if position else "queued"
            elif view.get("stage"):
                label = str(view["stage"])
            if (label != shown
                    and time.monotonic() - last_post >= MIN_UPDATE_SECS):
                shown = label
                last_post = time.monotonic()
                status = await self._update(status, f"yt: {label}…")

            if waited >= TIMEOUT:
                await self._update(
                    status,
                    f"yt: still working after {TIMEOUT / 60:.0f}min. the job "
                    f"({job_id}) will keep running in the cloud; check `>ytdlp queue` later 😁",
                )
                return None
            await asyncio.sleep(POLL_SECS)
            waited += POLL_SECS

    async def _deliver(self, invocation: Invocation, status: discord.Message,
                       result: dict):
        url = str(result.get("url") or "")
        if not url:
            await self._fail(status, "worker finished but sent no link 😟")
            return
        embed = discord.Embed(
            title=str(result.get("title") or "download")[:250],
            url=str(result.get("webpage_url") or url),
            color=0x5865F2,
        )
        if result.get("uploader"):
            embed.set_author(name=str(result["uploader"])[:100])
        fields = [
            ("file", f"{result.get('size_mb', '?')}mb · "
                     f"{'mp3' if result.get('audio') else 'mp4'}"),
            ("duration", str(result.get("duration") or "?")),
        ]
        for name, value in fields:
            if value and value != "?":
                embed.add_field(name=name, value=value, inline=True)
        embed.set_footer(text="leech · cloud ytdlp")
        thumb = str(result.get("thumbnail") or "")
        if thumb.startswith("https"):
            embed.set_thumbnail(url=thumb)
        try:
            await status.delete()
        except (discord.HTTPException, TypeError):
            pass
        await invocation.message.reply(
            content=url,
        )

    async def _update(self, status: discord.Message,
                      text: str) -> discord.Message:
        try:
            fresh = await status.channel.send(text)
        except discord.HTTPException:
            return status
        try:
            await status.delete()
        except (discord.HTTPException, TypeError):
            pass
        return fresh

    async def _fail(self, status: discord.Message, message: str):
        try:
            await status.channel.send(f"yt failed 😟: {message}")
        except discord.HTTPException:
            _logger.warning("yt failed (status gone) 😟: %s", message)
        try:
            await status.delete()
        except (discord.HTTPException, TypeError):
            pass


def create_cog(context):
    return YtdlpCog(context)

