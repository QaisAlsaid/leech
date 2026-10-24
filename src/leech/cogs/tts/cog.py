from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import discord
import httpx

from leech.core.cog.cog import Cog, CogContext
from leech.core.cog.error import CogError
from leech.core.command.runtime import Command, Argument
from leech.core.command.native.binding import NativeBinding
from leech.core.command.mock.binding import MockBinding
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.helpers.attachments.attachments import gather_attachments

try:
    import soundfile as sf

    _HAS_SF = True
except ImportError:
    _HAS_SF = False

_logger = logging.getLogger(__name__)

TTS_STORE = Path("persistent/v1/tts")
VOICES_DIR = TTS_STORE / "voices"
VOICES_STORE = TTS_STORE / "voices.json"
SAMPLE_MAX_BYTES = int(os.getenv("LEECH_TTS_SAMPLE_MAX_MB", "20")) * 1024 * 1024
URL_RE = re.compile(r"https?://\S+")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MIN_SAMPLE_SECONDS = 5.2
TRIM_SECONDS = 20

MANAGER = (os.getenv("LEECH_TTS_URL") or "").strip().rstrip("/")
TOKEN = (os.getenv("LEECH_TTS_TOKEN") or "").strip()
TIMEOUT = float(os.getenv("LEECH_TTS_TIMEOUT", "600") or 600)
MAX_CHARS = int(os.getenv("LEECH_TTS_MAX_CHARS", "2000") or 2000)
POLL_SECS = 3.0
MIN_UPDATE_SECS = 8.0
UPLOAD_LIMIT = 10 * 1024 * 1024


class _ManagerError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # offline | auth | bad | server
        self.message = message


class RemoteTtsCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.voices: dict[str, dict] = {}  # name -> {"file": str, "text": str|None}

    async def load(self):
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        if VOICES_STORE.exists():
            try:
                data = json.loads(VOICES_STORE.read_text(encoding="utf-8"))
                raw = data.get("voices", {})
                for name, entry in raw.items():
                    if isinstance(entry, str):  # v1 shape
                        entry = {"file": entry, "text": None}
                    self.voices[str(name)] = {
                        "file": str(entry.get("file", "")),
                        "text": entry.get("text"),
                    }
            except (json.JSONDecodeError, OSError):
                _logger.warning("remote tts: voices.json unreadable, starting empty.")
        for name, command in {
            **self.get_tts(),
        }.items():
            if not self.context.add_command(name, command):
                raise CogError(f"i failed to load '{name}' tts command.")
        if not MANAGER:
            _logger.warning("remote tts: LEECH_TTS_URL is not set  `>tts` unavilable.")

    def get_tts(self) -> dict[str, Command]:
        return {
            "tts": Command(
                name="tts",
                description="speak text through kitten tts.",
                usage_example=">tts bored bard bird",
                help_content=(
                    "sends your text to the manager's queue. the cloud worker picks "
                    "it, and the clip comes back as an attachment. "
                    "vc=<name> uses a clone saved with "
                    "`>tts voice save` (or a "
                    "kitten builtin voice name). kitten expression tags "
                    "([joyful] <laugh> (((emphasis))) and preset=expressive) "
                    "pass through."
                ),
                arguments=[
                    Argument(
                        name="text",
                        description="what to say.",
                        usage_example="hello there niggas",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="vc",
                        description="saved clone name (default: the model's own voice).",
                        usage_example="vc=qais",
                        type=TypesRegistry.require(Types.STR),
                        required=False,
                    ),
                    Argument(
                        name="preset",
                        description='decoding preset, e.g. "expressive" (kitten engine).',
                        usage_example="preset=expressive",
                        type=TypesRegistry.require(Types.STR),
                        required=False,
                    ),
                ],
                subcommands=self.get_clone()
                | self.get_voice()
                | self.get_queue()
                | self.get_kill()
                | self.get_drain(),
                binding=NativeBinding(handler=self.tts_handler),
            )
        }

    def get_clone(self) -> dict[str, Command]:
        return {
            "clone": Command(
                name="clone",
                description="speak text in an attached / linked / replied to voice, once (voice never saved).",
                usage_example=">tts clone hi it's me it's varity ask me anything",
                help_content=(
                    "one shot voice clone: attach a sample or paste a media "
                    "link or reply to a message that has one, and pass the "
                    "text to say. the sample is normalized and trimmed to its "
                    "first 20 seconds and sent as a job. nothing is "
                    "saved. it **must** be longer than 5 seconds. pass "
                    "`transcript=(what the clip says)` for the best clone "
                    "quality. without one the worker **TRIES** to transcribes the sample "
                    "itself. (WHISPER COULD FAIL AND CAUSE THE WORKER TO CRASH)."
                ),
                arguments=[
                    Argument(
                        name="text",
                        description="what to say.",
                        usage_example="hi it's me it's varity",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="transcript",
                        description="what the sample says.",
                        usage_example='transcript="hey its me is qaisity"',
                        type=TypesRegistry.require(Types.STR),
                        required=False,
                    ),
                ],
                subcommands={},
                binding=NativeBinding(handler=self.clone_handler),
            )
        }

    def get_voice(self) -> dict[str, Command]:
        return {
            "voice": Command(
                name="voice",
                description="manage saved voice clones and the cloud queue.",
                usage_example=">tts voice save name=qais_1",
                arguments=[],
                subcommands={
                    **self.get_voice_save(),
                    **self.get_voice_remove(),
                    **self.get_voice_list(),
                    **self.get_voice_sync(),
                },
                binding=MockBinding(),
                is_root=True,
            )
        }

    def get_voice_save(self) -> dict[str, Command]:
        return {
            "save": Command(
                name="save",
                description="save a voice sample as a named clone.",
                usage_example=">tts voice save name=qais_1",
                help_content="the sample comes from this message's attachment or a "
                "media link in it or the message you replied to. it "
                "**must** be longer than 5 seconds. pass `transcript=(with what's said in the clip)` "
                "for the best clone quality. without it the worker "
                "**TRIES** transcribes the sample itself. (WHISPER COULD FAIL AND CAUSE THE WORKER TO CRASH).",
                arguments=[
                    Argument(
                        name="name",
                        description="clone name (lowercase letters, digits, -, _).",
                        usage_example="name=qais_1",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="transcript",
                        description="what is said in the sample.",
                        usage_example='transcript="hey its me its qaisity"',
                        type=TypesRegistry.require(Types.STR),
                        required=False,
                    ),
                ],
                subcommands={},
                binding=NativeBinding(handler=self.voice_save_handler),
            )
        }

    def get_voice_remove(self) -> dict[str, Command]:
        return {
            "remove": Command(
                name="remove",
                description="remove a saved voice clone.",
                usage_example=">tts voice remove name=qais_1",
                help_content="deletes it locally and on the manager.",
                arguments=[
                    Argument(
                        name="name",
                        description="the clone to remove.",
                        usage_example="name=qais_1",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                ],
                subcommands={},
                binding=NativeBinding(handler=self.voice_remove_handler),
            )
        }

    def get_voice_list(self) -> dict[str, Command]:
        return {
            "list": Command(
                name="list",
                description="list saved voice clones.",
                usage_example=">tts voice list",
                help_content="your saved clones (local + cloud registry).",
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.voice_list_handler),
            )
        }

    def get_voice_sync(self) -> dict[str, Command]:
        return {
            "sync": Command(
                name="sync",
                description="push all local clones to the cloud manager.",
                usage_example=">tts voice sync",
                help_content=(
                    "the cloud worker fetches voices from the manager by "
                    "name. this reuploads every clone you have saved "
                    "locally. (only run it after deploying a fresh manager, "
                    "or after editing files by hand)."
                ),
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.voice_sync_handler),
            )
        }

    def get_queue(self) -> dict[str, Command]:
        return {
            "queue": Command(
                name="queue",
                description="cloud queue + worker status.",
                usage_example=">tts queue",
                help_content=(
                    "asks the manager: jobs queued, worker sessions alive, "
                    "which engine they run.\n(if there is no worker listed, activate cloud worker)."
                ),
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.voice_ping_handler),
            )
        }

    def get_kill(self) -> dict[str, Command]:
        return {
            "kill": Command(
                name="kill",
                description="cancel a queued/running cloud job (tts or yt).",
                usage_example=">tts kill id=18f2a1b4",
                help_content=(
                    "kills one job on the manager queue - tts OR yt. the id "
                    "(or any unique prefix of it) comes from `>tts queue`. "
                    "the person waiting on it sees it fail as 'cancelled'. "
                    "everyone can kill for now - be nice."
                ),
                arguments=[
                    Argument(
                        name="id",
                        description="job id or its unique prefix.",
                        usage_example="id=18f2a1b4",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    )
                ],
                subcommands={},
                binding=NativeBinding(handler=self.kill_handler),
            ),
        }

    def get_drain(self) -> dict[str, Command]:
        return {
            "drain": Command(
                name="drain",
                description="cancel every queued cloud job (tts and yt).",
                usage_example=">tts drain inflight=on",
                help_content=(
                    "nukes the whole queue. queued jobs die instantly; "
                    "running ones die too with inflight=on (their workers "
                    "keep computing, but the result is discarded). use "
                    "`>tts queue` first to see what you're about to kill."
                ),
                arguments=[
                    Argument(
                        name="inflight",
                        description="also cancel jobs already running (off by default).",
                        usage_example="inflight=on",
                        type=TypesRegistry.require(Types.BOOL),
                        required=False,
                    )
                ],
                subcommands={},
                binding=NativeBinding(handler=self.drain_handler),
            ),
        }

    async def tts_handler(self, invocation: Invocation):
        self._require_manager()
        text = str(invocation.arguments["text"]).strip()
        if not text:
            raise CogError("nothing to say 😁", is_user_error=True)
        if len(text) > MAX_CHARS:
            raise CogError(
                f"text is {len(text)} chars, keep it under {MAX_CHARS}.",
                is_user_error=True,
            )
        voice = str(
            invocation.arguments.get("vc") or invocation.arguments.get("voice") or ""
        ).strip()
        preset = str(invocation.arguments.get("preset") or "").strip() or None

        payload: dict = {
            "text": text,
            "preset": preset,
            "requested_by": invocation.message.author.id,
        }
        label = "default voice"
        if voice:
            entry = self._clone_entry(voice)
            if entry is not None:
                await self._ensure_voice_pushed(voice, entry)
                if entry.get("text"):
                    payload["reference_text"] = entry["text"]
                label = f"clone `{voice}`"
            payload["voice"] = voice

        await self._run_job(
            invocation, payload, label, name=f"tts-{voice or 'default'}"
        )

    async def clone_handler(self, invocation: Invocation):
        self._require_manager()
        text = str(invocation.arguments["text"]).strip()
        if not text:
            raise CogError("nothing to say 😁", is_user_error=True)
        if len(text) > MAX_CHARS:
            raise CogError(
                f"text is {len(text)} chars, keep it under {MAX_CHARS}.",
                is_user_error=True,
            )
        sample, source = await self._resolve_sample(invocation.message)
        wav = await self._to_wav_async(sample)
        seconds = self._probe_seconds_bytes(wav)
        if seconds is not None and seconds <= MIN_SAMPLE_SECONDS:
            raise CogError(
                f"the sample is {seconds:.1f}s. cloning needs more than 5 "
                "seconds (5-15s best).",
                is_user_error=True,
            )
        raw = invocation.arguments.get("transcript")
        transcript = str(raw).strip() if raw is not None else ""
        opus = await self._to_opus_async(wav)
        payload = {
            "text": text,
            "reference_b64": base64.b64encode(opus).decode("ascii"),
        }
        if transcript:
            payload["reference_text"] = transcript
        label = f"cloning from {source}"
        await self._run_job(invocation, payload, label, name="tts-clone")

    async def voice_save_handler(self, invocation: Invocation):
        self._require_manager()
        name = str(invocation.arguments["name"]).strip().lower()
        if not NAME_RE.match(name):
            raise CogError(
                "clone names are lowercase letters or digits or `-` and `_` (max is 32).",
                is_user_error=True,
            )
        if name in self.voices:
            raise CogError(
                f"`{name}` already exists try `>tts voice remove name={name}` first 😁",
                is_user_error=True,
            )
        raw = invocation.arguments.get("transcript")
        transcript = str(raw).strip() if raw is not None else ""
        sample, _source = await self._resolve_sample(invocation.message)
        wav = await self._to_wav_async(sample)
        seconds = self._probe_seconds_bytes(wav)
        if seconds is not None and seconds <= MIN_SAMPLE_SECONDS:
            raise CogError(
                f"the sample is {seconds:.1f}s. cloning needs more than 5 "
                "seconds (5-15s best).",
                is_user_error=True,
            )
        filename = f"{name}.wav"
        (VOICES_DIR / filename).write_bytes(wav)
        self.voices[name] = {"file": filename, "text": transcript or None}
        self._save_voices()

        pushed = await self._push_voice(
            name, await self._to_opus_async(wav), seconds, transcript or None
        )
        note = "" if pushed else " (cloud push failed. try `>tts voice sync` later)"
        warn = (
            ""
            if transcript
            else (
                " · no transcript. the worker will transcribe it itself "
                "(ALLOW_WHISPER=1)"
            )
        )
        await invocation.message.reply(
            f"saved voice `{name}`"
            + (f" ({seconds:.1f}s)" if seconds else "")
            + f"{note}{warn}. use: `>tts vc={name} <text>`",
            mention_author=False,
        )

    async def voice_remove_handler(self, invocation: Invocation):
        name = str(invocation.arguments["name"]).strip().lower()
        entry = self.voices.pop(name, None)
        if entry is None:
            raise CogError(f"no saved voice: `{name}`.", is_user_error=True)
        (VOICES_DIR / entry["file"]).unlink(missing_ok=True)
        self._save_voices()
        if MANAGER and TOKEN:
            try:
                await self._call("DELETE", f"/api/voices/{name}")
            except _ManagerError as e:
                _logger.warning("voice delete on manager failed: %s", e.message)
        await invocation.message.reply(f"removed voice `{name}`.")

    async def voice_list_handler(self, invocation: Invocation):
        if not self.voices:
            await invocation.message.reply(
                "no saved clones. attach/reply/link a sample that is larger than 5 seconds "
                "and run `>tts voice save name=<n>` 😁",
                mention_author=False,
            )
            return
        names = ", ".join(f"`{n}`" for n in sorted(self.voices))
        await invocation.message.reply(f"saved voices: {names}", mention_author=False)

    async def voice_sync_handler(self, invocation: Invocation):
        self._require_manager()
        if not self.voices:
            await invocation.message.reply(
                "nothing to sync. no saved clones 😁", mention_author=False
            )
            return
        status = await invocation.message.reply(
            "syncing clones to the manager...", mention_author=False
        )
        ok, failed = 0, []
        for name, entry in sorted(self.voices.items()):
            wav_path = VOICES_DIR / entry["file"]
            if not wav_path.exists():
                failed.append(f"{name} (file missing)")
                continue
            opus = await self._to_opus_async(wav_path.read_bytes())
            seconds = self._probe_seconds_bytes(wav_path.read_bytes())
            if await self._push_voice(name, opus, seconds, entry.get("text")):
                ok += 1
            else:
                failed.append(name)
        text = f"synced {ok}/{len(self.voices)} clones."
        if failed:
            text += f" failed: {', '.join(failed)} 😟"
        await self._update_status(status, text)

    def _kaggle_lines(self, kag: dict, has_workers: bool, queued: int) -> str:
        if not kag or not kag.get("configured"):
            return ""
        bits: list[str] = []
        last = kag.get("last_wake") or {}
        if last:
            try:
                age = max(int(time.time() - float(last.get("at") or 0)), 0)
            except (TypeError, ValueError):
                age = 0
            wake_bits = [f"last wake {age // 60}m ago"]
            if last.get("version"):
                wake_bits.append(f"v{last['version']}")
            wake_bits.append("gpu" if last.get("gpu") else "cpu")
            if last.get("reason"):
                wake_bits.append(str(last["reason"])[:44])
            if last.get("error"):
                wake_bits.append(f"err {str(last['error'])[:50]}")
            bits.append(" · ".join(wake_bits))
        else:
            bits.append("never woken")
        if kag.get("cooldown_left"):
            bits.append(f"cooldown {int(kag['cooldown_left'])}s")
        # v9: the pixeldrain flag from the manager - answers "is the key
        # even set there" without opening vercel.
        if "pixeldrain" in kag:
            bits.append(
                "pixeldrain"
                if kag.get("pixeldrain")
                else "pixeldrain: NO KEY on manager"
            )
        out = f"\nkaggle: {' · '.join(bits)}"
        if queued and not has_workers:
            out += (
                "\nhint: jobs waiting + no worker -> the next >tts awakes "
                "the kernel."
            )
        return out

    async def voice_ping_handler(self, invocation: Invocation):
        self._require_manager()
        try:
            data = await self._call_json("GET", "/api/status")
        except _ManagerError as e:
            await invocation.message.reply(
                f"manager unreachable 😟: {e.message}", mention_author=False
            )
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
            worker_text = (
                "no worker session alive. jobs will queue until one is activated."
            )
        voices = data.get("voices") or []
        kag_lines = self._kaggle_lines(
            data.get("kaggle") or {}, bool(workers), data.get("queued") or 0
        )
        jobs_block = await self._queue_block()
        await invocation.message.reply(
            f"```\nqueued: {data.get('queued')}\ninflight: {data.get('inflight')}"
            f"\nvoices on manager: {len(voices)}"
            f"\nworkers:\n{worker_text}{kag_lines}\n```"
            + (f"\njobs:\n{jobs_block}" if jobs_block else ""),
            mention_author=False,
        )

    async def _queue_block(self) -> str:
        """v10.6: the actual jobs sitting on the manager (tts AND yt -
        one queue), so `>tts queue` answers 'what exactly is stuck' and
        gives you the ids `>tts kill` wants. best-effort - a manager that
        doesn't know /api/queue yet just shows nothing."""
        try:
            data = await self._call_json("GET", "/api/queue")
        except _ManagerError:
            return ""
        rows: list[str] = []
        for job in (data.get("queued") or [])[:15]:
            rows.append(self._job_row(job, "queued"))
        for job in (data.get("inflight") or [])[:15]:
            rows.append(self._job_row(job, "inflight"))
        total_q = len(data.get("queued") or [])
        total_i = len(data.get("inflight") or [])
        if not rows:
            return ""
        hidden = (total_q + total_i) - len(rows)
        block = "\n".join(rows)
        if hidden > 0:
            block += f"\n...and {hidden} more (killed just the same by `>tts drain`)"
        return block

    @staticmethod
    def _job_row(job: dict, state: str) -> str:
        kind = str(job.get("kind") or "?")
        jid = str(job.get("id") or "")
        age = job.get("age")
        age_s = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
        preview = str(job.get("preview") or "")[:48]
        worker = str(job.get("worker") or "")
        if state == "queued":
            pos = job.get("position")
            where = f"pos {pos}" if pos else "?"
        else:
            stage = str(job.get("stage") or "")[:24]
            where = f"{worker[:12]}:{stage}" if worker else stage or "?"
        return f"- `[{kind}]` `{jid}` {state} ({where}, {age_s}) {preview}"

    async def kill_handler(self, invocation: Invocation):
        self._require_manager()
        ident = str(invocation.arguments["id"]).strip()
        try:
            result = await self._call_json("DELETE", f"/api/jobs/{ident}")
        except _ManagerError as e:
            raise CogError(f"kill failed: {e.message}", is_user_error=True)
        if result.get("already"):
            await invocation.message.reply(
                f"job `{result.get('id') or ident}` already finished "
                f"({result['already']}) - nothing to kill.",
                mention_author=False,
            )
            return
        await invocation.message.reply(
            f"cancelled `[{result.get('kind') or '?'}]` job "
            f"`{result.get('id') or ident}` 😁",
            mention_author=False,
        )

    async def drain_handler(self, invocation: Invocation):
        self._require_manager()
        inflight = bool(invocation.arguments.get("inflight"))
        try:
            result = await self._call_json(
                "POST", "/api/queue/drain", {"kill_inflight": inflight}
            )
        except _ManagerError as e:
            raise CogError(f"drain failed: {e.message}", is_user_error=True)
        killed = result.get("cancelled") or 0
        running = result.get("inflight_cancelled") or 0
        if not killed and not running:
            await invocation.message.reply(
                "the queue was already empty.", mention_author=False
            )
            return
        note = f" (+{running} running job(s) cancelled)" if running else ""
        await invocation.message.reply(
            f"drained {killed} queued job(s){note}.", mention_author=False
        )

    def _require_manager(self):
        if not MANAGER:
            raise CogError(
                "LEECH_TTS_URL isn't set. fix the bot, and point it at the manager.",
                is_user_error=False,
            )

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if TOKEN:
            headers["x-leech-token"] = TOKEN
        return headers

    async def _call(
        self, method: str, path: str, json_body: dict | None = None
    ) -> httpx.Response:
        timeout = httpx.Timeout(connect=15.0, read=60.0, write=180.0, pool=15.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    return await client.get(MANAGER + path, headers=self._headers())
                if method == "DELETE":
                    return await client.delete(MANAGER + path, headers=self._headers())
                return await client.post(
                    MANAGER + path, json=json_body, headers=self._headers()
                )
        except httpx.HTTPError as e:
            raise _ManagerError(
                "offline", f"can't reach the manager ({e.__class__.__name__})"
            )

    async def _call_json(
        self, method: str, path: str, json_body: dict | None = None
    ) -> dict:
        response = await self._call(method, path, json_body)
        if response.status_code == 401:
            raise _ManagerError(
                "auth", "token rejected. LEECH_TTS_TOKEN vs manager TOKEN"
            )
        if response.status_code >= 400:
            raise _ManagerError("server", f"manager said {response.status_code}")
        return response.json()

    async def _push_voice(
        self, name: str, opus: bytes, seconds: float | None, text: str | None
    ) -> bool:
        body = {
            "name": name,
            "data_b64": base64.b64encode(opus).decode("ascii"),
            "seconds": seconds,
            "text": text,
        }
        try:
            await self._call_json("POST", "/api/voices", body)
            return True
        except _ManagerError as e:
            _logger.warning("voice push failed for %s: %s", name, e.message)
            return False

    async def _ensure_voice_pushed(self, name: str, entry: dict):
        try:
            response = await self._call("GET", f"/api/voices/{name}")
            if response.status_code == 200:
                return
        except _ManagerError:
            return
        wav_path = VOICES_DIR / entry["file"]
        if not wav_path.exists():
            return
        opus = await self._to_opus_async(wav_path.read_bytes())
        await self._push_voice(
            name,
            opus,
            self._probe_seconds_bytes(wav_path.read_bytes()),
            entry.get("text"),
        )

    def _wake_note(self, wake) -> str:
        if not isinstance(wake, dict) or not wake.get("status"):
            return ""
        state = str(wake.get("status"))
        reason = str(wake.get("reason") or "")
        if state == "pushed":
            return (
                f" · waking kaggle "
                f"({'gpu' if wake.get('gpu') else 'cpu'}, "
                f"v{wake.get('version') or '?'})"
            )
        if state == "booting":
            return f" · kaggle: booting ({reason[:70]})"
        if state == "error":
            return f" · kaggle wake failed: {reason[:80]}"
        if reason:
            return f" · kaggle: {state} ({reason[:50]})"
        return f" · kaggle: {state}"

    async def _run_job(
        self, invocation: Invocation, payload: dict, label: str, name: str
    ):
        try:
            job = await self._call_json(
                "POST", "/api/jobs", {"kind": "tts", "payload": payload}
            )
        except _ManagerError as e:
            raise CogError(f"manager: {e.message}", is_user_error=False)
        job_id = job.get("id")
        position = job.get("position") or 1
        wake_note = self._wake_note(job.get("wake"))
        status = await invocation.message.reply(
            f"tts: queued (position {position}). {label}{wake_note}",
            mention_author=False,
        )

        # v9: the wait loop below was lost in the "kiggle wakes handle"
        # splice (commit 6dad3a9) - the bot queued jobs, replied "queued"
        # and then never polled again, so finished clips never arrived.
        # restored verbatim from the v8 cog: poll the job, edit the status
        # through stages, deliver the audio when the worker posts it.
        shown = f"queued (position {position})"
        last_post = time.monotonic()
        waited = 0.0
        while True:
            try:
                view = await self._call_json("GET", f"/api/jobs/{job_id}")
            except _ManagerError as e:
                await self._fail_status(status, e.message)
                return
            state = view.get("status")
            if state == "done":
                result = view.get("result") or {}
                data, ext = await self._result_bytes(result)
                if data is None:
                    await self._fail_status(
                        status,
                        "the worker finished but sent no audio back 😟 "
                        f"({result.get('engine') or '?'} · "
                        f"{result.get('gen_seconds') or '?'}s)",
                    )
                    return
                engine = str(result.get("engine") or "?")
                gen = result.get("gen_seconds")
                footer = f" · {engine}" + (f" · {gen}s" if gen else "")
                await self._deliver(invocation, status, data, ext, name, footer)
                return
            if state == "error":
                await self._fail_status(status, str(view.get("error") or "?"))
                return

            stage = str(view.get("stage") or "")
            if state == "queued":
                pos = view.get("position")
                stage = f"queued (position {pos})" if pos else "queued"
            if (
                stage
                and stage != shown
                and time.monotonic() - last_post >= MIN_UPDATE_SECS
            ):
                shown = stage
                last_post = time.monotonic()
                status = await self._update_status(status, f"tts: {stage}. {label}...")
            if waited >= TIMEOUT:
                no_worker = ""
                try:
                    page = await self._call_json("GET", "/api/status")
                    if not page.get("workers"):
                        no_worker = (
                            " no worker session is alive - the next "
                            ">tts re-wakes kaggle on its own"
                        )
                except _ManagerError:
                    pass
                await self._update_status(
                    status,
                    f"tts: still queued after {TIMEOUT / 60:.0f}min "
                    f"(job {job_id}).{no_worker}",
                )
                return
            await asyncio.sleep(POLL_SECS)
            waited += POLL_SECS

    async def _result_bytes(self, result: dict) -> tuple[bytes | None, str]:
        ext = str(result.get("ext") or "mp3")
        if result.get("audio_b64"):
            try:
                return base64.b64decode(str(result["audio_b64"])), ext
            except Exception:
                return None, ext
        url = str(result.get("url") or "")
        if url.startswith("http"):
            try:
                async with httpx.AsyncClient(
                    timeout=120.0, follow_redirects=True
                ) as client:
                    response = await client.get(url)
                response.raise_for_status()
                if len(response.content) > UPLOAD_LIMIT:
                    return None, ext
                return response.content, ext
            except httpx.HTTPError:
                return None, ext
        return None, ext

    async def _resolve_sample(self, message: discord.Message) -> tuple[bytes, str]:
        found = await self._find_sample(message)
        if found is not None:
            return found
        ref = getattr(message, "reference", None)
        if ref is not None and ref.message_id is not None:
            try:
                origin = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                raise CogError(
                    "couldn't fetch the message you replied to 😟.", is_user_error=True
                )
            found = await self._find_sample(origin)
            if found is not None:
                return found
        raise CogError(
            "no voice sample found: attach one, paste a media link, "
            "or reply to a message that has one 😁",
            is_user_error=True,
        )

    async def _find_sample(self, message: discord.Message) -> tuple[bytes, str] | None:
        attachments = await gather_attachments(message)
        if attachments:
            first = attachments[0]
            return await self._read_attachment(first), getattr(
                first, "filename", "sample.bin"
            )
        urls = URL_RE.findall(message.content or "")
        if urls:
            data = await self._download(urls[0])
            if data is not None:
                name = urls[0].split("/")[-1].split("?")[0] or "sample.bin"
                return data, name
        return None

    async def _read_attachment(self, attachment) -> bytes:
        if hasattr(attachment, "read"):
            return await attachment.read()
        file = await attachment.to_file()
        if hasattr(file.fp, "read"):
            return file.fp.read()
        with open(file.fp, "rb") as fh:
            return fh.read()

    async def _download(self, url: str) -> bytes | None:
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                if len(response.content) > SAMPLE_MAX_BYTES:
                    raise CogError(
                        f"sample is over {SAMPLE_MAX_BYTES // 1024 // 1024}mb. "
                        "trim it to 5-15 seconds 😁",
                        is_user_error=True,
                    )
                return response.content
        except CogError:
            raise
        except httpx.HTTPError:
            return None

    async def _to_wav_async(self, data: bytes) -> bytes:
        if not shutil.which("ffmpeg"):
            return data
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                "pipe:0",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-t",
                str(TRIM_SECONDS),
                "-f",
                "wav",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate(data)
            return out if proc.returncode == 0 and out else data
        except OSError:
            return data

    async def _to_opus_async(self, wav: bytes) -> bytes:
        if not shutil.which("ffmpeg"):
            return wav
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                "pipe:0",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-t",
                str(TRIM_SECONDS),
                "-codec:a",
                "libopus",
                "-b:a",
                "32k",
                "-f",
                "ogg",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate(wav)
            return out if proc.returncode == 0 and out else wav
        except OSError:
            return wav

    def _probe_seconds_bytes(self, wav: bytes) -> float | None:
        if not _HAS_SF:
            return None
        try:
            info = sf.info(io.BytesIO(wav))
            return float(info.duration)
        except Exception:
            return None

    async def _deliver(
        self,
        invocation: Invocation,
        status: discord.Message,
        data: bytes,
        ext: str,
        name: str,
        footer: str = "",
    ):
        if len(data) > UPLOAD_LIMIT:
            await self._update_status(
                status,
                f"the clip is {len(data) / 1024 / 1024:.1f}mb. over "
                "upload limit. try shorter text 😁",
            )
            return
        try:
            await status.delete()
        except (discord.HTTPException, TypeError):
            pass
        await invocation.message.reply(
            file=discord.File(io.BytesIO(data), filename=f"{name}.{ext}"),
            content=(f"_{footer}_" if footer else None),
            mention_author=False,
        )

    async def _update_status(
        self, status: discord.Message, text: str
    ) -> discord.Message:
        try:
            fresh = await status.channel.send(text)
        except discord.HTTPException:
            return status
        await self._delete_status(status)
        return fresh

    async def _delete_status(self, status: discord.Message):
        try:
            await status.delete()
        except (discord.HTTPException, TypeError):
            pass

    async def _fail_status(self, status: discord.Message, message: str):
        try:
            await status.channel.send(f"tts failed 😟: {message}")
        except discord.HTTPException:
            _logger.warning("tts failed (status gone) 😟: %s", message)
        await self._delete_status(status)

    def _clone_entry(self, name: str) -> dict | None:
        entry = self.voices.get(name.strip().lower())
        if entry is None:
            return None
        if not (VOICES_DIR / entry["file"]).exists():
            return None  # stale entry
        return entry

    def _save_voices(self):
        TTS_STORE.mkdir(parents=True, exist_ok=True)
        VOICES_STORE.write_text(
            json.dumps({"version": 2, "voices": self.voices}, indent=2),
            encoding="utf-8",
        )


def create_cog(context):
    return RemoteTtsCog(context)
