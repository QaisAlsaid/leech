from __future__ import annotations


import asyncio
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import discord
import httpx

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

HUB_URL = (os.getenv("LEECH_HUB_URL") or "").strip().rstrip("/")
HUB_TOKEN = (os.getenv("LEECH_HUB_TOKEN") or "").strip()
COLAB_URL = (os.getenv("LEECH_YT_URL") or os.getenv("LEECH_TTS_URL") or "").strip().rstrip("/")
COLAB_TOKEN = (os.getenv("LEECH_YT_TOKEN") or os.getenv("LEECH_TTS_TOKEN") or "").strip()
PROVIDERS_ENV = (os.getenv("LEECH_YT_PROVIDERS") or "colab,hub").strip()
# v9: the built-in lane order flipped to colab-first. the hub runner kept
# bot-check-failing on youtube (datacenter ip) so every job paid the
# fail-tax before falling back to kaggle, which just worked. per-job
# provider= still pins, LEECH_YT_PROVIDERS still overrides, and
# `>ytdlp default` pins it persistently now.
TIMEOUT = float(os.getenv("LEECH_YT_TIMEOUT", "1800") or 1800)
MAX_MB = float(os.getenv("LEECH_YT_MAX_MB", "200") or 200)
POLL_SECS = 2.5
PROGRESS_MIN_SECS = 3.0
REPLY_LIMIT = 1990
COOKIE_PATH = Path("persistent/v1/ytdlp/cookies.txt")
PREFS_PATH = Path("persistent/v1/ytdlp/prefs.json")
COOKIE_MAX_BYTES = 200_000
# v9: finished files <= this get re-uploaded as a real discord attachment
# (inline player, the notsobot .dl behavior). bigger ones stay links -
# pixeldrain links once PIXELDRAIN_KEY rides the worker.
ATTACH_LIMIT = int(os.getenv("LEECH_YT_ATTACH_MB", "10") or 10) * 1024 * 1024

_URL_RE = re.compile(r"^https?://\S+$")
_PROVIDER_RE = re.compile(r"^(hub|colab)$", re.IGNORECASE)


class YtError(BaseError):
    def __init__(self, message: str, is_user_error: bool = False):
        super().__init__(message=message, is_user_error=is_user_error)


class _LaneFail(Exception):
    def __init__(self, lane: str, message: str):
        super().__init__(message)
        self.lane = lane
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



def _validate_cookie_text(data: bytes) -> tuple[str, int, int]:
    if len(data) > COOKIE_MAX_BYTES:
        raise CogError(
            f"that file is {len(data) // 1000}kb - the cap is "
            f"{COOKIE_MAX_BYTES // 1000}kb. export fewer sites (youtube only).",
            is_user_error=True)
    try:
        text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise CogError("cookies.txt must be plain text (utf-8).",
                       is_user_error=True)
    if "\x00" in text:
        raise CogError("that file has null bytes in it - not a cookies.txt.",
                       is_user_error=True)
    lines = 0
    domains = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        lines += 1
        domains.add(fields[0].strip().lower())
    if not lines:
        raise CogError(
            "no valid cookie lines found - netscape format is 7 columns "
            "separated by TABs. export with a 'Get cookies.txt' extension.",
            is_user_error=True)
    if not any("youtube" in d or "google" in d or "yt" == d for d in domains):
        _logger.warning("cookie file has no youtube/google rows (domains=%d)",
                        len(domains))
    if not text.endswith("\n"):
        text += "\n"
    return text, lines, len(domains)


class _CookieStore:
    def __init__(self, path: Path = COOKIE_PATH):
        self._path = path

    def read(self) -> Optional[str]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        text = text.strip()
        return text or None

    def save(self, text: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".txt.temp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            raise YtError(f"couldn't save the cookie file: {exc}")

    def clear(self) -> bool:
        existed = self._path.exists()
        try:
            self._path.unlink(missing_ok=True)
            self._path.with_suffix(".txt.temp").unlink(missing_ok=True)
        except OSError:
            return existed
        return existed

    def stats(self) -> Optional[dict]:
        try:
            stat = self._path.stat()
            text = self.read()
        except OSError:
            return None
        if not text:
            return None
        lines = sum(1 for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                    and line.count("\t") >= 6)
        return {"size": stat.st_size, "lines": lines,
                "age_s": max(0, int(time.time() - stat.st_mtime))}


class _PrefStore:
    """v9: tiny persisted prefs (the `>ytdlp default` lane pin). same
    atomic-write pattern as the cookie store; one key, one file."""

    def __init__(self, path: Path = PREFS_PATH):
        self._path = path
        self._lock = threading.Lock()

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return default
            return str(data.get(key) or default)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data[key] = value
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".json.temp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError:
                pass  # pref is cosmetic - never fail a command over it


class _HubLane:
    name = "hub"
    pretty = "hub (gh runner)"

    def __init__(self):
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            if method == "GET":
                resp = await self._http.get(
                    HUB_URL + path, headers={"x-leech-token": HUB_TOKEN})
            else:
                resp = await self._http.post(
                    HUB_URL + path, json=body,
                    headers={"x-leech-token": HUB_TOKEN})
        except httpx.HTTPError as exc:
            raise _LaneFail("hub", f"leech-hub unreachable ({type(exc).__name__})")
        if resp.status_code == 401:
            raise _LaneFail("hub", "the hub rejected the token "
                                   "(LEECH_HUB_TOKEN).")
        if resp.status_code != 200:
            try:
                detail = str(resp.json()["error"])
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            raise _LaneFail("hub", f"hub answered {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise _LaneFail("hub", "the hub returned invalid json.")
        if not data.get("ok"):
            raise _LaneFail("hub", str(data.get("error") or "unknown hub error"))
        return data

    async def submit(self, payload: dict) -> str:
        data = await self._api("POST", "/api/jobs",
                               body={"kind": "yt", "payload": payload})
        return str(data["job"])

    async def view(self, job_id: str) -> dict:
        data = await self._api("GET", f"/api/jobs/{job_id}")
        job = dict(data.get("job") or {})
        progress = job.get("progress") or {}
        state = str(job.get("status") or "queued")
        if state == "queued":
            label = "queued"
        elif state == "inflight":
            stage = str(progress.get("stage") or "working")
            total = progress.get("total")
            if isinstance(total, int) and total > 0:
                label = f"{stage} {progress.get('done', 0)}/{total}"
            else:
                label = stage
            if progress.get("note"):
                label += f": {progress['note']}"
        else:
            label = state
        return {"status": state, "label": label,
                "result": job.get("result") or {},
                "error": job.get("error")}

    async def health(self) -> dict:
        return await self._api("GET", "/api/health")


class _ColabLane:
    name = "colab"
    pretty = "colab (manager)"

    def __init__(self):
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if COLAB_TOKEN:
            headers["x-leech-token"] = COLAB_TOKEN
        return headers

    async def _call(self, method: str, path: str,
                    json_body: dict | None = None) -> httpx.Response:
        try:
            if method == "GET":
                return await self._http.get(COLAB_URL + path,
                                            headers=self._headers())
            return await self._http.post(COLAB_URL + path, json=json_body,
                                         headers=self._headers())
        except httpx.HTTPError as exc:
            raise _LaneFail("colab", f"the manager is unreachable "
                                     f"({type(exc).__name__}) - is the colab "
                                     f"notebook running?")

    async def submit(self, payload: dict) -> str:
        response = await self._call("POST", "/api/jobs",
                                    {"kind": "yt", "payload": payload})
        if response.status_code == 401:
            raise _LaneFail("colab", "token rejected. LEECH_YT_TOKEN vs the "
                                     "manager's TOKEN.")
        if response.status_code == 400:
            try:
                detail = str(response.json().get("detail") or "?")
            except Exception: 
                detail = "?"
            raise _LaneFail("colab", detail)
        if response.status_code >= 400:
            raise _LaneFail("colab", f"manager said {response.status_code}.")
        try:
            job_id = str(response.json().get("id") or "")
        except json.JSONDecodeError:
            raise _LaneFail("colab", "the manager returned invalid json.")
        if not job_id:
            raise _LaneFail("colab", "the manager didn't give the job an id.")
        return job_id

    async def view(self, job_id: str) -> dict:
        response = await self._call("GET", f"/api/jobs/{job_id}")
        if response.status_code == 404:
            raise _LaneFail("colab", "job gone - maybe it expired "
                                     "(older than a day).")
        if response.status_code >= 400:
            raise _LaneFail("colab", f"manager said {response.status_code}.")
        try:
            view = response.json()
        except json.JSONDecodeError:
            raise _LaneFail("colab", "the manager returned invalid json.")
        state = str(view.get("status") or "queued")
        if state == "queued":
            position = view.get("position")
            label = f"queued (position {position})" if position else "queued"
        elif state == "inflight" and view.get("stage"):
            label = str(view["stage"])
        else:
            label = state
        return {"status": state, "label": label,
                "result": view.get("result") or {},
                "error": view.get("error")}

    async def health(self) -> dict:
        response = await self._call("GET", "/api/status")
        if response.status_code == 401:
            raise _LaneFail("colab", "token rejected (LEECH_YT_TOKEN).")
        if response.status_code >= 400:
            raise _LaneFail("colab", f"manager said {response.status_code}.")
        try:
            return response.json()
        except json.JSONDecodeError:
            raise _LaneFail("colab", "the manager returned invalid json.")

    async def wake(self, gpu: bool = False) -> dict:
        """ask the manager to wake the kaggle kernel right now (v6)."""
        response = await self._call(
            "POST", "/api/wake", {"force": True, "gpu": gpu})
        if response.status_code == 401:
            raise _LaneFail("colab", "token rejected (LEECH_YT_TOKEN).")
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail") or
                             response.json().get("error") or "?")
            except Exception:  # noqa: BLE001
                detail = f"manager said {response.status_code}."
            raise _LaneFail("colab", detail)
        try:
            return response.json()
        except json.JSONDecodeError:
            raise _LaneFail("colab", "the manager returned invalid json.")


_LANES = {
    "hub": lambda: _HubLane(),
    "colab": lambda: _ColabLane(),
}


def _lane_env_ok(name: str) -> bool:
    if name == "hub":
        return bool(HUB_URL and HUB_TOKEN)
    return bool(COLAB_URL and COLAB_TOKEN)


def _provider_order(forced: Optional[str], pinned: str = "") -> list[str]:
    if forced:
        return [forced]
    # v9: `>ytdlp default colab|hub` pins the lane persistently; auto
    # (or a missing pref) rides the env order, colab-first now.
    if pinned in ("hub", "colab"):
        return [pinned]
    order = []
    for name in PROVIDERS_ENV.split(","):
        name = name.strip().lower()
        if name in _LANES and name not in order:
            order.append(name)
    return [name for name in order if _lane_env_ok(name)] or \
           [name for name in ("hub", "colab") if _lane_env_ok(name)]


class YtdlpCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        if not (_lane_env_ok("hub") or _lane_env_ok("colab")):
            _logger.warning(
                "ytdlp cog: no lane configured (LEECH_HUB_URL/TOKEN for the "
                "hub, LEECH_YT_URL/TOKEN for colab). not loading"
            )
            return
        self.context.add_on_load(self.load)
        self.context.add_on_unload(self.shutdown)
        self.lanes = {name: factory()
                      for name, factory in _LANES.items() if _lane_env_ok(name)}
        self.cookies = _CookieStore()
        self.prefs = _PrefStore()

    async def shutdown(self) -> None:
        for lane in getattr(self, "lanes", {}).values():
            await lane.aclose()

    async def load(self):
        root = Command(
            name="ytdlp",
            description="fetch a video/audio link - hub or colab, whichever works.",
            usage_example=">ytdlp https://youtu.be/YUqCwBa2iIk",
            help_content=(
                "downloads it on whichever lane is alive - the "
                "(hub) or the colab/kaggle worker. and the finished file "
                "lands on the store chain. if the first lane fails it "
                "retries on the other automatically; `provider=hub` or "
                "`provider=colab` pins one. video defaults to 720p mp4, "
                "audio=on gets only mp3, quality= caps video height (144-2160), "
                "max_mb= is the size cap (default 200), start=/end= in "
                "seconds or mm:ss cuts a clip. youtube blocking the runner? "
                "attach a cookies.txt to `>ytdlp cookies` and it rides "
                "every job (burner account). `>ytdlp queue` shows both "
                "lanes; `>ytdlp cookies clear` forgets the cookie file."
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
                Argument(
                    name="provider",
                    description="pin the lane: hub or colab.",
                    usage_example="provider=colab",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
            ],
            subcommands={
                "queue": Command(
                    name="queue",
                    description="both lanes: queue + workers.",
                    usage_example=">ytdlp queue",
                    help_content=(
                        "asks both lanes how deep their queue is and who's "
                        "alive - the hub's runner dispatch plus the "
                        "colab/kaggle workers checking into the manager. "
                        "whichever answers is the one your next download "
                        "will ride."
                    ),
                    arguments=[],
                    subcommands={},
                    binding=NativeBinding(handler=self.queue_handler),
                ),
                "default": Command(
                    name="default",
                    description="show or pin the default lane.",
                    usage_example=">ytdlp default lane=colab",
                    help_content=(
                        "without arguments: shows the current default lane "
                        "and the full try-order. `lane=colab` sends every "
                        "download to the manager queue (colab/kaggle "
                        "workers), `lane=hub` to the github runner, "
                        "`lane=auto` rides the env order (colab first). "
                        "the pin survives restarts; per-job provider= "
                        "still beats it."
                    ),
                    arguments=[
                        Argument(
                            name="lane",
                            description="colab | hub | auto.",
                            usage_example="lane=colab",
                            type=TypesRegistry.require(Types.STR),
                            required=False,
                        ),
                    ],
                    subcommands={},
                    binding=NativeBinding(handler=self.default_handler),
                ),
                "wake": Command(
                    name="wake",
                    description="wake the kaggle kernel now (drains, then idles out).",
                    usage_example=">ytdlp wake",
                    help_content=(
                        "forces a kaggle kernel push right now, skipping the "
                        "manager's cooldown. the kernel boots (~2-5 min), "
                        "drains the queue, idles 15 min, then kills itself "
                        "so quota isn't wasted. `gpu=on` makes it a gpu "
                        "kernel (tts-ready, burns the 30h/week gpu quota - "
                        "the default is cpu, best for yt). automatic wakes "
                        "happen anyway when jobs land with no worker alive; "
                        "this is the manual way."
                    ),
                    arguments=[
                        Argument(
                            name="gpu",
                            description="on = gpu kernel (tts).",
                            usage_example="gpu=on",
                            type=TypesRegistry.require(Types.BOOL),
                            required=False,
                        ),
                    ],
                    subcommands={},
                    binding=NativeBinding(handler=self.wake_handler),
                ),
                "cookies": Command(
                    name="cookies",
                    description="store a cookies.txt for youtube (attach it).",
                    usage_example=">ytdlp cookies",
                    help_content=(
                        "attach a netscape cookies.txt to this command and "
                        "it gets stored for the bot - every future yt job "
                        "carries it to the runner/colab so youtube stops "
                        "asking for a login. no attachment = status. "
                        "`>ytdlp cookies clear` forgets it. use a burner "
                        "account: the file IS a live login, and yt "
                        "sometimes flags automation accounts."
                    ),
                    arguments=[
                        Argument(
                            name="action",
                            description="clear - forget the stored cookies.",
                            usage_example="clear",
                            type=TypesRegistry.require(Types.STR),
                            required=False,
                        ),
                    ],
                    subcommands={},
                    binding=NativeBinding(handler=self.cookies_handler),
                    elevated=True
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

    async def _edit(self, status: discord.Message, text: str) -> None:
        try:
            await status.edit(content=text[:REPLY_LIMIT])
        except discord.HTTPException:
            pass

    def _cookie_payload(self) -> dict:
        try:
            text = self.cookies.read()
        except Exception:
            text = None
        return {"cookies": text} if text else {}

    async def _await_lane(self, lane, job_id: str, note: str,
                          status: discord.Message) -> dict:
        shown = ""
        last_edit = 0.0
        deadline = time.monotonic() + TIMEOUT
        while True:
            view = await lane.view(job_id)
            state = view["status"]
            if state == "done":
                result = dict(view.get("result") or {})
                if not result:
                    raise _LaneFail(lane.name, "finished but sent no link 😟")
                return result
            if state == "error":
                raise _LaneFail(lane.name, str(view.get("error") or
                                               "the job failed on the lane."))
            label = f"{note} - {lane.name}: {view['label']}"
            now = time.monotonic()
            if label != shown and now - last_edit >= PROGRESS_MIN_SECS:
                await self._edit(status, label)
                shown, last_edit = label, now
            if now >= deadline:
                raise _LaneFail(
                    lane.name,
                    f"still working after {TIMEOUT / 60:.0f}min (the job "
                    f"({job_id}) keeps running in the cloud)")
            await asyncio.sleep(POLL_SECS)

    # ------------------------------------------------------------- handlers

    async def yt_handler(self, invocation: Invocation):
        url = str(invocation.arguments["url"]).strip()
        if not _URL_RE.match(url):
            raise CogError("invalid link 😁", is_user_error=True)

        forced = str(invocation.arguments.get("provider") or "").strip().lower()
        if forced and not _PROVIDER_RE.match(forced):
            raise CogError("provider must be hub or colab.",
                           is_user_error=True)
        order = _provider_order(forced, self.prefs.get("default_lane"))
        if not order:
            raise YtError("no yt lane is configured - ask qais to fix leech "
                          "(LEECH_HUB_* / LEECH_YT_*).")

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
        payload.update(self._cookie_payload())

        note = "yt: audio (mp3)" if payload.get("audio") else \
               f"yt: video ({payload.get('quality') or 720}p)"

        status_msg = await self._reply(invocation.message, f"{note} - queued...")
        failures: list[tuple[str, str]] = []
        for index, lane_name in enumerate(order):
            lane = self.lanes.get(lane_name)
            if lane is None:
                failures.append((lane_name, "lane not configured"))
                continue
            try:
                if index == 0:
                    await self._edit(status_msg, f"{note} - {lane_name}: submitting...")
                job_id = await lane.submit(payload)
                result = await self._await_lane(lane, job_id, note, status_msg)
            except _LaneFail as exc:
                failures.append((lane_name, exc.message))
                _logger.warning("yt lane '%s' failed: %s", lane_name, exc.message)
                if index + 1 < len(order):
                    await self._edit(
                        status_msg,
                        f"{note} - {lane_name} failed "
                        f"({exc.message[:120]}) - trying {order[index + 1]}...")
                continue
            await self._deliver(invocation, status_msg, result, lane_name)
            return

        parts = [f"{name}: {msg[:160]}" for name, msg in failures]
        raise YtError("every lane failed - " + " · ".join(parts))

    async def default_handler(self, invocation: Invocation):
        """v9: `>ytdlp default` - show or persistently pin the lane."""
        raw = str(invocation.arguments.get("lane") or "").strip().lower()
        if not raw:
            pinned = self.prefs.get("default_lane") or "auto"
            order = _provider_order(None, self.prefs.get("default_lane"))
            trail = " -> ".join(order) if order else "none configured"
            await self._reply(invocation.message,
                              f"default lane: **{pinned}** · try order: {trail}")
            return
        if raw not in ("colab", "hub", "auto"):
            raise CogError("lane must be colab, hub or auto.",
                           is_user_error=True)
        if raw in ("hub", "colab") and self.lanes.get(raw) is None:
            raise CogError(f"the {raw} lane isn't configured on this bot.",
                           is_user_error=True)
        self.prefs.set("default_lane", raw)
        order = _provider_order(None, raw)
        trail = " -> ".join(order) if order else "none configured"
        await self._reply(
            invocation.message,
            f"default lane pinned: **{raw}** · try order: {trail}"
            + (" (provider= on a single job still overrides)" if raw != "auto" else ""),
        )

    async def queue_handler(self, invocation: Invocation):
        lines = ["```"]
        for name in ("hub", "colab"):
            lane = self.lanes.get(name)
            if lane is None:
                lines.append(f"{name:6s}  not configured")
                continue
            try:
                if name == "hub":
                    health = await lane.health()
                    runner = health.get("runner") or {}
                    if runner.get("dispatch"):
                        state = f"armed ({runner.get('repo') or '?'})"
                    else:
                        state = "dispatch off - janitor sweeps every 15 min"
                    lines.append(f"hub    queued: {health.get('queue_depth', 0)}"
                                 f" · runner: {state}")
                else:
                    page = await lane.health()
                    workers = [w for w in (page.get("workers") or [])
                               if isinstance(w, dict)]
                    if workers:
                        who = "; ".join(
                            f"{w.get('id') or '?'}"
                            + (f", {w.get('gpu')}" if w.get("gpu") else "")
                            + f" ({w.get('last_seen_ago', '?')}s ago)"
                            for w in workers[:4])
                    else:
                        who = "none - start the colab notebook or >ytdlp wake"
                    lines.append(f"colab  queued: {page.get('queued', 0)}"
                                 f" · inflight: {page.get('inflight', 0)}"
                                 f" · workers: {who}")
                    kaggle = page.get("kaggle") or {}
                    if kaggle.get("configured"):
                        bits = [f"kernel {kaggle.get('kernel') or '?'}"]
                        last = kaggle.get("last_wake") or {}
                        if last:
                            try:
                                age = int(time.time() - float(last.get("at") or 0))
                            except (TypeError, ValueError):
                                age = 0
                            wake_bits = [f"last wake {max(age, 0) // 60}m ago"]
                            if last.get("version"):
                                wake_bits.append(f"v{last['version']}")
                            wake_bits.append("gpu" if last.get("gpu") else "cpu")
                            if last.get("error"):
                                wake_bits.append(f"err: {str(last['error'])[:60]}")
                            bits.append(" · ".join(wake_bits))
                        else:
                            bits.append("never woken")
                        if kaggle.get("cooldown_left"):
                            bits.append(f"cooldown {int(kaggle['cooldown_left'])}s")
                        lines.append(f"kaggle {' · '.join(bits)}")
                    elif kaggle:
                        missing = ", ".join(kaggle.get("missing") or ["?"])
                        lines.append(f"kaggle off (missing: {missing[:70]})")
                    kworkers = [w for w in (page.get("workers") or [])
                                if isinstance(w, dict)
                                and str(w.get("id") or "").startswith("kaggle")]
                    if kworkers:
                        lines.append("        (a kaggle kernel is alive and "
                                     "checking in right now)")
            except _LaneFail as exc:
                lines.append(f"{name:6s}  unreachable - {exc.message[:100]}")
        cookie = self.cookies.stats()
        if cookie:
            lines.append(f"cookies: {cookie['lines']} lines, "
                         f"{cookie['age_s'] // 3600}h old - sent on every job")
        pinned = self.prefs.get("default_lane") or "auto"
        lines.append(f"default lane: {pinned}"
                     + ("" if pinned == "auto" else "  (>ytdlp default lane=auto to reset)"))
        lines.append("```")
        await self._reply(invocation.message, "\n".join(lines))

    async def wake_handler(self, invocation: Invocation):
        lane = self.lanes.get("colab")
        if lane is None:
            raise CogError("wake needs the colab lane (LEECH_YT_URL / "
                           "LEECH_YT_TOKEN on the bot).",
                           is_user_error=True)
        gpu = bool(invocation.arguments.get("gpu"))
        await self._reply(invocation.message,
                          f"waking kaggle ({'gpu' if gpu else 'cpu'}) - "
                          "the kernel takes ~2-5 min to boot...")
        try:
            info = await lane.wake(gpu=gpu)
        except _LaneFail as exc:
            raise YtError(f"the wake failed - {exc.message}")
        state = str(info.get("status") or "?")
        if state == "pushed":
            version = info.get("version") or "?"
            await self._reply(
                invocation.message,
                f"kernel pushed (version {version}, "
                f"{'gpu' if info.get('gpu') else 'cpu'}) - it will check in "
                "as a worker within a few minutes. `>ytdlp queue` to watch.")
            return
        reason = str(info.get("reason") or info.get("error") or "?")
        await self._reply(
            invocation.message,
            f"wake: {state} - {reason[:300]}")

    async def cookies_handler(self, invocation: Invocation):
        action = str(invocation.arguments.get("action") or "").strip().lower()
        if action == "clear":
            cleared = self.cookies.clear()
            await self._reply(
                invocation.message,
                "cookie file forgotten" if cleared else
                "no cookie file was stored anyway.")
            return
        if action:
            raise CogError("the only cookies sub-action is `clear` - attach "
                           "a cookies.txt to store one.",
                           is_user_error=True)

        attachments = list(getattr(invocation.message, "attachments", []) or [])
        if not attachments:
            stats = self.cookies.stats()
            if not stats:
                await self._reply(
                    invocation.message,
                    "no cookie file stored. attach a netscape cookies.txt "
                    "(export it with a 'Get cookies.txt' extension while "
                    "logged in on a BURNER account) to this command to "
                    "store one - it rides every yt job to both lanes.")
            else:
                await self._reply(
                    invocation.message,
                    f"cookies stored: {stats['lines']} lines, "
                    f"{stats['size'] // 1024}kb, "
                    f"{stats['age_s'] // 3600}h old - sent on every yt job "
                    "to hub and colab. `>ytdlp cookies clear` forgets it.")
            return

        attachment = attachments[0]
        if attachment.size and attachment.size > COOKIE_MAX_BYTES:
            raise CogError(
                f"that file is {attachment.size // 1000}kb - the cap is "
                f"{COOKIE_MAX_BYTES // 1000}kb.", is_user_error=True)
        try:
            data = await attachment.read()
        except discord.HTTPException as exc:
            raise YtError(f"couldn't read the attachment: {exc}")
        text, lines, domains = _validate_cookie_text(data)
        self.cookies.save(text)
        await self._reply(
            invocation.message,
            f"cookies stored {lines} lines across {domains} domains - "
            "every yt job carries them youtube logins expire, so "
            "re-export when downloads start failing again. (burner account).")

    async def _deliver(self, invocation: Invocation, status: discord.Message, result: dict, lane_name: str):
          url = str(result.get("url") or "")
          if not url:
            await self._fail(status, "the lane finished but sent no link 😟")
            return

          # v9: files small enough for discord ride back as real
          # attachments - inline video/audio player, the notsobot .dl
          # behavior. big ones stay links (a user line is ~10mb).
          data = await self._fetch_for_attach(url, result)

          size = result.get("size_mb")
          if size is None and result.get("size"):
            try:
              size = round(float(result["size"]) / 1e6, 1)
            except (TypeError, ValueError):
              size = None

          title = str(result.get("title") or "download")[:250]
          webpage_url = str(result.get("webpage_url") or url)
          uploader = str(result.get("uploader"))[:100] if result.get("uploader") else None

          size_str = f"{size}mb" if size is not None else "?mb"
          format_str = (
            result.get("format") or ("mp3" if result.get("audio") else "mp4")
          )
          duration_str = self._fmt_duration(result.get("duration"))

          lines = [f"**[{title}]({webpage_url})**"]

          if uploader:
            lines.append(f" {uploader}")

          details = []
          if size_str != "?mb":
            details.append(size_str)
          if format_str:
            details.append(str(format_str))
          if duration_str:
            details.append(duration_str)

          if details:
            lines.append(" · ".join(details))

          lines.append(f"leech · yt · {lane_name}")
          lines.append(url)

          message_text = "\n".join(lines)

          try:
            await status.delete()
          except (discord.HTTPException, TypeError):
            pass

          if data is not None:
            await invocation.message.reply(
              content=message_text,
              file=discord.File(io.BytesIO(data),
                                filename=self._attach_name(result)),
              mention_author=False,
              allowed_mentions=discord.AllowedMentions.none(),
            )
            return

          await invocation.message.reply(
            content=message_text,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
          )

    def _attach_name(self, result: dict) -> str:
        title = str(result.get("title") or "download")
        stem = re.sub(r"[^\w\-. ]+", "_", title).strip().replace(" ", "_")[:80]
        stem = stem.strip("._") or "download"
        url = str(result.get("url") or "")
        dot = url.rsplit("/", 1)[-1].rpartition(".")[2].lower()
        ext = dot if dot in ("mp4", "mp3", "mkv", "webm", "m4a", "ogg", "wav") \
            else ("mp3" if result.get("audio") else "mp4")
        return f"{stem}.{ext}"

    async def _fetch_for_attach(self, url: str, result: dict) -> Optional[bytes]:
        """download the finished file when it fits the attachment budget.
        streams so an oversized file aborts early instead of eating ram;
        any hiccup just falls back to the plain link."""
        size = result.get("size_mb")
        try:
            known_mb = float(size)
        except (TypeError, ValueError):
            known_mb = None
        if known_mb is not None and known_mb * 1024 * 1024 > ATTACH_LIMIT:
            return None
        try:
            chunks: list[bytes] = []
            held = 0
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(240.0, connect=20.0),
                    follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        held += len(chunk)
                        if held > ATTACH_LIMIT:
                            return None
                        chunks.append(chunk)
            data = b"".join(chunks)
            return data if data else None
        except (httpx.HTTPError, OSError):
            return None

    @staticmethod
    def _fmt_duration(value) -> str:
        if isinstance(value, str) and ":" in value:
            return value
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
