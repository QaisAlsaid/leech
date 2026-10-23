from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence
from urllib.parse import urlparse

import httpx

from leech.core.error import BaseError
from leech.core.store import Store, get_store, StoreError

_logger = logging.getLogger(__name__)

# v11: the ai cog's persistence rides the store. row shapes match the
# migrator: ai_personas kv {channel_id: prompt}, ai_config doc
# {active, muted, window_budget, chain}. the audit trail is the
# append-only ai_log ns.
NS_PERSONAS = "ai_personas"
NS_CONFIG = "ai_config"


class AiError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


DEFAULT_PERSONA = (
    "you are leech, a dry but helpful, low key, assistant living inside a discord bot, chatting "
    "with a couple of humans. keep replies short and conversational, plain text. "
    "you may run the bot's commands on request."
)

TUNNEL_B64_MAX = 3_400_000
DEGRADE_NOTE = (
    "\n(the attached image could not be processed. answering without seeing it)"
)
AI_CHAIN_FALLBACK = "groq:openai/gpt-oss-120b,gemini:gemini-3.5-flash-lite"
GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GROQ_API = "https://api.groq.com/openai/v1"
THINKING_EFFORT = {"low": "low", "high": "high"}


def _env_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for name, env in (("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY")):
        value = (os.getenv(env) or "").strip()
        if value:
            keys[name] = value
    return keys


def _default_chain() -> list[list[str]]:
    chain: list[list[str]] = []
    for entry in (os.getenv("LEECH_AI_CHAIN") or AI_CHAIN_FALLBACK).split(","):
        provider, _, model = entry.strip().partition(":")
        if provider and model:
            chain.append([provider, model])
    return chain


@dataclass
class Turn:
    author: str
    author_id: int
    content: str
    is_bot: bool = False
    was_replying_to: Optional[str] = None


@dataclass
class Attachment:
    filename: str
    content_type: Optional[str]
    size: int
    data: bytes
    viewable: bool


class ContextWindow:
    def __init__(self, limit: int = 60, budget: int = 8_000, turn_cap: int = 800):
        self._turns: deque[Turn] = deque(maxlen=limit)
        self._budget = budget  # total rendered chars; the TPM-sensitive knob
        self._turn_cap = turn_cap

    def push(
        self,
        author: str,
        author_id: int,
        content: str,
        *,
        is_bot: bool = False,
        reply: Optional[str] = None,
    ) -> None:
        content = content.strip()[: self._turn_cap]
        if not content:
            return
        self._turns.append(
            Turn(
                author=author,
                author_id=author_id,
                content=content,
                is_bot=is_bot,
                was_replying_to=reply,
            )
        )
        self._trim()

    def render(self) -> list[tuple[bool, str]]:
        out: list[tuple[bool, str]] = []
        for turn in self._turns:
            if turn.is_bot:
                text = turn.content
            else:
                text = f"{turn.author} (id: {turn.author_id}): {turn.content}"
            if turn.was_replying_to:
                whose = " your message" if turn.is_bot else ""
                text = f"[was replying to{whose}: {turn.was_replying_to}]\n{text}"
            out.append((turn.is_bot, text))
        return out

    def clear(self) -> None:
        self._turns.clear()

    def size(self) -> int:
        return len(self._turns)

    def chars(self) -> int:
        return self._size()

    def budget(self) -> int:
        return self._budget

    def set_budget(self, chars: int) -> None:
        self._budget = chars
        self._trim()

    def _size(self) -> int:
        return sum(
            len(t.content) + len(t.author) + len(t.was_replying_to or "")
            for t in self._turns
        )

    def _trim(self) -> None:
        while len(self._turns) > 1 and self._size() > self._budget:
            self._turns.popleft()


@dataclass(frozen=True)
class Generation:
    temperature: float = 0.6
    max_tokens: int = 1024
    thinking: str = "low"


class ManagerAi:
    def __init__(self, base_url: str, token: str, *, timeout: float = 90.0):
        self._base = base_url.rstrip("/")
        self._headers = {"x-leech-token": token} if token else {}
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0, write=240.0, pool=15.0)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> httpx.Response:
        url = self._base + path
        for attempt in range(2):
            started = time.monotonic()
            try:
                if method == "GET":
                    response = await self._http.get(url, headers=self._headers)
                else:
                    response = await self._http.post(
                        url, json=body, headers=self._headers
                    )
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                if attempt == 0 and time.monotonic() - started < 5.0:
                    await asyncio.sleep(0.75)
                    continue
                raise AiError(f"ai service unreachable: {type(e).__name__}") from e
            if (
                response.status_code >= 500
                and attempt == 0
                and time.monotonic() - started < 5.0
            ):
                await asyncio.sleep(0.75)
                continue
            return response
        raise AiError("ai service unreachable")

    @staticmethod
    def _fail(response: httpx.Response) -> AiError:
        if response.status_code == 401:
            return AiError(
                "manager rejected the ai token (LEECH_AI_TOKEN vs manager TOKEN)"
            )
        try:
            detail = str(response.json()["detail"])
        except Exception:
            detail = response.text[:300]
        return AiError(f"manager answered {response.status_code}: {detail}")

    async def chat(
        self,
        *,
        system: str,
        turns: list[tuple[bool, str]],
        attachments: Sequence[Attachment],
        generation: Generation,
    ) -> tuple[str, bool, str]:
        body = {
            "system": system,
            "turns": [{"bot": is_bot, "text": text} for is_bot, text in turns],
            "attachments": [
                {
                    "filename": a.filename,
                    "mime": a.content_type,
                    "data": base64.b64encode(a.data).decode("ascii"),
                }
                for a in attachments
            ],
            "generation": {
                "temperature": generation.temperature,
                "max_tokens": generation.max_tokens,
                "thinking": generation.thinking,
            },
        }
        response = await self._request("POST", "/api/ai/chat", body)
        if response.status_code != 200:
            raise self._fail(response)
        data = response.json()
        if not data.get("ok"):
            raise AiError(str(data.get("error") or "unknown manager error"))
        return (
            str(data.get("text") or ""),
            bool(data.get("vision")),
            str(data.get("slot") or "?"),
        )

    async def chain(self) -> dict:
        response = await self._request("GET", "/api/ai/chain")
        if response.status_code != 200:
            raise self._fail(response)
        return response.json()

    async def _chain_action(self, path: str, body: dict) -> None:
        response = await self._request("POST", path, body)
        if response.status_code != 200:
            raise self._fail(response)

    async def chain_add(
        self, provider: str, model: str, position: Optional[int] = None
    ) -> None:
        await self._chain_action(
            "/api/ai/chain/add",
            {"provider": provider, "model": model, "position": position},
        )

    async def chain_remove(self, key: str) -> None:
        await self._chain_action("/api/ai/chain/remove", {"key": key})

    async def chain_promote(self, key: str) -> None:
        await self._chain_action("/api/ai/chain/main", {"key": key})

    async def chain_reset(self) -> None:
        await self._chain_action("/api/ai/chain/reset", {})


class _ManagerBackend:
    def __init__(self, client: ManagerAi):
        self._client = client

    @property
    def name(self) -> str:
        try:
            netloc = urlparse(self._client._base).netloc
        except Exception:
            netloc = "manager"
        return f"cloud ({netloc})" if netloc else "cloud (manager)"

    async def chat(self, *, system, turns, attachments, generation):
        return await self._client.chat(
            system=system,
            turns=turns,
            attachments=attachments,
            generation=generation,
        )

    async def usage(self) -> tuple[list[str], dict]:
        data = await self._client.chain()
        lines: list[str] = []
        for index, slot in enumerate(data.get("chain", [])):
            state = (
                f"cooling ({slot.get('cooldown_left', 0)}s left)"
                if slot.get("cooling")
                else "ready"
            )
            vision = " · vision" if slot.get("vision") else ""
            mark = " (main)" if index == 0 else ""
            lines.append(f"`{slot['key']}`{mark}{vision}: {state}")
        if not lines:
            lines.append(
                "(no models. set GEMINI_API_KEY / GROQ_API_KEY on the manager)"
            )
        return lines, data.get("daily") or {}

    async def add_slot(self, provider: str, model: str, position=None) -> str:
        await self._client.chain_add(provider, model, position)
        return f"{provider}:{model}"

    async def remove_slot(self, key: str) -> None:
        await self._client.chain_remove(key)

    async def promote_slot(self, key: str) -> None:
        await self._client.chain_promote(key)

    async def reset_chain(self) -> None:
        await self._client.chain_reset()

    def chain_state(self) -> Optional[list]:
        return None

    async def aclose(self) -> None:
        await self._client.aclose()


class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


class Provider:
    name: str = "?"
    vision: bool = False

    def __init__(self, api_key: str, *, read_timeout: float = 20.0):
        self._key = api_key
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(read_timeout, connect=5.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    def _request(self, model, system, turns, generation, attachments=()):
        raise NotImplementedError

    def _extract(self, data: dict) -> str:
        raise NotImplementedError

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            return f"{response.status_code}: {response.json()['error']['message']}"
        except Exception:
            return str(response.status_code)

    async def complete(
        self,
        model: str,
        *,
        system: str,
        turns: list[tuple[bool, str]],
        generation: Generation,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        url, headers, body = self._request(
            model, system, turns, generation, attachments
        )
        response = await self._post(url, headers, body)
        if response.status_code >= 400:
            raise ProviderError(
                response.status_code, f"{self.name} answered {self._detail(response)}"
            )
        try:
            return self._extract(response.json())
        except (KeyError, IndexError, TypeError):
            raise ProviderError(
                0, f"{self.name} returned an unusable payload"
            ) from None

    async def _post(self, url: str, headers: dict, body: dict) -> httpx.Response:
        for attempt in range(2):
            try:
                response = await self._http.post(url, headers=headers, json=body)
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                raise ProviderError(
                    0, f"{self.name} unreachable: {type(e).__name__}"
                ) from e
            if 400 <= response.status_code < 500:
                raise ProviderError(
                    response.status_code,
                    f"{self.name} answered {self._detail(response)}",
                )
            if response.status_code >= 500:
                if attempt == 0:
                    await asyncio.sleep(0.75)
                    continue
                raise ProviderError(
                    response.status_code,
                    f"{self.name} answered {self._detail(response)}",
                )
            return response
        raise ProviderError(0, "unreachable")


class GeminiProvider(Provider):
    name = "gemini"
    vision = True

    def _request(self, model, system, turns, generation, attachments=()):
        last = len(turns) - 1
        contents = []
        for index, (is_bot, text) in enumerate(turns):
            parts: list[dict] = [{"text": text}]
            if attachments and index == last and not is_bot:
                for att in attachments:
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": att.content_type or "image/png",
                                "data": base64.b64encode(att.data).decode("ascii"),
                            }
                        }
                    )
            contents.append({"role": "model" if is_bot else "user", "parts": parts})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {
                "response_mime_type": "application/json",
                "maxOutputTokens": generation.max_tokens,
                "temperature": generation.temperature,
            },
        }
        headers = {"x-goog-api-key": self._key}
        return GEMINI_API.format(model=model), headers, body

    def _extract(self, data: dict) -> str:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise KeyError("empty response")
        return text


class OpenAICompatProvider(Provider):
    def __init__(self, name: str, api_key: str, base_url: str, **kwargs):
        super().__init__(api_key, **kwargs)
        self.name = name
        self._base = base_url.rstrip("/")

    def _request(self, model, system, turns, generation, attachments=()):
        messages: list[dict] = [{"role": "system", "content": system}]
        last = len(turns) - 1
        for index, (is_bot, text) in enumerate(turns):
            content: object = text
            if attachments and index == last and not is_bot:
                content = [{"type": "text", "text": text}]
                for att in attachments:
                    b64 = base64.b64encode(att.data).decode("ascii")
                    mime = att.content_type or "image/png"
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        }
                    )
            messages.append(
                {"role": "assistant" if is_bot else "user", "content": content}
            )
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": generation.temperature,
            "max_tokens": generation.max_tokens,
            "response_format": {"type": "json_object"},
        }
        if generation.thinking in THINKING_EFFORT:
            body["reasoning_effort"] = THINKING_EFFORT[generation.thinking]
        headers = {"Authorization": f"Bearer {self._key}"}
        return f"{self._base}/chat/completions", headers, body

    def _extract(self, data: dict) -> str:
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            raise KeyError("empty response")
        return text


PROVIDERS: dict = {
    "gemini": GeminiProvider,
    "groq": lambda key, **kw: OpenAICompatProvider("groq", key, GROQ_API, **kw),
}


@dataclass(frozen=True)
class Slot:
    provider: Provider
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider.name}:{self.model}"


class _DirectBackend:
    """the local half: providers, chain and cooldowns on the bot itself."""

    def __init__(self, *, turn_deadline: float = 75.0, keys: Optional[dict] = None):
        self._keys = keys if keys is not None else _env_keys()
        self._read_timeout = float(os.getenv("LEECH_AI_READ_TIMEOUT", "20") or 20)
        self._cooldown = float(os.getenv("LEECH_AI_COOLDOWN", "90") or 90)
        self._deadline = turn_deadline
        self._cooldowns: dict[str, float] = {}
        self._slots: list[Slot] = []
        self._persist_hook = None
        self._build(_default_chain())

    def set_persist_hook(self, hook) -> None:
        self._persist_hook = hook

    def _persist(self) -> None:
        if self._persist_hook:
            self._persist_hook()

    def _build(self, chain: list) -> None:
        slots: list[Slot] = []
        seen: set[str] = set()
        for provider_name, model in chain:
            key = f"{provider_name}:{model}"
            if (
                provider_name in PROVIDERS
                and provider_name in self._keys
                and model
                and key not in seen
            ):
                slots.append(self._create_slot(provider_name, model))
                seen.add(key)
        self._slots = slots

    def _create_slot(self, provider_name: str, model: str) -> Slot:
        provider = PROVIDERS[provider_name](
            self._keys[provider_name], read_timeout=self._read_timeout
        )
        return Slot(provider=provider, model=model)

    async def _rebuild(self, chain: list) -> None:
        old = {slot.key: slot for slot in self._slots}
        new_slots: list[Slot] = []
        for provider_name, model in chain:
            key = f"{provider_name}:{model}"
            if key in old:
                new_slots.append(old.pop(key))
                continue
            new_slots.append(self._create_slot(provider_name, model))
        for leftover in old.values():
            await leftover.provider.aclose()
        self._slots = new_slots

    def restore_chain(self, entries: Optional[list]) -> None:
        if not entries:
            return
        chain: list[list[str]] = []
        for entry in entries:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                chain.append([str(entry[0]), str(entry[1])])
        if chain:
            self._build(chain)

    def chain_state(self) -> Optional[list]:
        return [[slot.provider.name, slot.model] for slot in self._slots]

    def _chain(self) -> list[tuple[str, str]]:
        return [(slot.provider.name, slot.model) for slot in self._slots]

    def _validate_entry(self, provider_name: str, model: str) -> None:
        if not model:
            raise AiError(
                "missing model. use provider:model, e.g. groq:openai/gpt-oss-120b 😁"
            )
        if provider_name not in PROVIDERS:
            raise AiError(
                f"unknown provider '{provider_name}' (known: {', '.join(sorted(PROVIDERS))})."
            )
        if provider_name not in self._keys:
            raise AiError(f"no api key configured for provider '{provider_name}' 😟")

    def _cooling(self, key: str) -> bool:
        return time.monotonic() < self._cooldowns.get(key, 0.0)

    async def chat(self, *, system, turns, attachments, generation):
        if not self._slots:
            raise AiError(
                "no models configured. set GEMINI_API_KEY / GROQ_API_KEY "
                "(and optionally LEECH_AI_CHAIN) on the bot."
            )
        text = None
        slot_key = "?"
        vision_used = False
        if attachments:
            ready = [
                s for s in self._slots if s.provider.vision and not self._cooling(s.key)
            ]
            if ready:
                try:
                    text, slot_key = await self._attempt(
                        ready, system, turns, attachments, generation
                    )
                    vision_used = True
                except AiError:
                    pass
        if text is None:
            if attachments and turns:
                is_bot, last_text = turns[-1]
                turns = turns[:-1] + [(is_bot, last_text + DEGRADE_NOTE)]
            text, slot_key = await self._attempt(
                list(self._slots), system, turns, [], generation
            )
        return text, vision_used, slot_key

    async def _attempt(self, slots, system, turns, attachments, generation):
        failures: list[str] = []
        deadline = time.monotonic() + self._deadline
        for slot in slots:
            if self._cooling(slot.key):
                failures.append(f"{slot.key}: cooling")
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                failures.append(f"{slot.key}: out of time")
                continue
            try:
                text = await asyncio.wait_for(
                    slot.provider.complete(
                        slot.model,
                        system=system,
                        turns=turns,
                        generation=generation,
                        attachments=attachments,
                    ),
                    timeout=remaining,
                )
            except ProviderError as e:
                if e.rate_limited:
                    self._cooldowns[slot.key] = time.monotonic() + self._cooldown
                _logger.warning("ai slot failed: %s: %s", slot.key, e.message)
                failures.append(f"{slot.key}: {e.message}")
                continue
            except asyncio.TimeoutError:
                self._cooldowns[slot.key] = time.monotonic() + self._cooldown
                _logger.warning("ai slot timed out: %s", slot.key)
                failures.append(f"{slot.key}: exceeded turn deadline")
                continue
            return text, slot.key
        raise AiError("every model failed. " + "; ".join(failures) + ".")

    async def usage(self) -> tuple[list[str], dict]:
        now = time.monotonic()
        lines: list[str] = []
        for index, slot in enumerate(self._slots):
            until = self._cooldowns.get(slot.key, 0.0)
            state = f"cooling ({until - now:.0f}s left)" if now < until else "ready"
            vision = " · vision" if slot.provider.vision else ""
            mark = " (main)" if index == 0 else ""
            lines.append(f"`{slot.key}`{mark}{vision}: {state}")
        if not lines:
            lines.append("(no models. set GEMINI_API_KEY / GROQ_API_KEY on the bot 😁)")
        return lines, {}

    async def add_slot(self, provider_name: str, model: str, position=None) -> str:
        self._validate_entry(provider_name, model)
        key = f"{provider_name}:{model}"
        if any(s.key == key for s in self._slots):
            raise AiError(f"'{key}' is already in the chain 😁")
        chain = self._chain()
        entry = (provider_name, model)
        if position is None:
            chain.append(entry)
        else:
            chain.insert(max(0, min(position, len(chain))), entry)
        await self._rebuild(chain)
        self._persist()
        return key

    async def remove_slot(self, key: str) -> None:
        chain = self._chain()
        remaining = [e for e in chain if f"{e[0]}:{e[1]}" != key]
        if len(remaining) == len(chain):
            raise AiError(f"'{key}' is not in the chain 😁")
        if not remaining:
            raise AiError("can't remove the last model in the chain.")
        await self._rebuild(remaining)
        self._cooldowns.pop(key, None)
        self._persist()

    async def promote_slot(self, key: str) -> None:
        chain = self._chain()
        entry = next((e for e in chain if f"{e[0]}:{e[1]}" == key), None)
        if entry is None:
            raise AiError(f"'{key}' is not in the chain 😁")
        chain.remove(entry)
        chain.insert(0, entry)
        await self._rebuild(chain)
        self._persist()

    async def reset_chain(self) -> None:
        await self._rebuild(_default_chain())
        self._cooldowns.clear()
        self._persist()

    async def aclose(self) -> None:
        for slot in self._slots:
            await slot.provider.aclose()

    @property
    def name(self) -> str:
        return "direct (bot egress)"


class _MissingBackend:
    def __init__(self, mode: str):
        self._mode = mode

    @property
    def name(self) -> str:
        return "none (unconfigured)"

    def _fail(self) -> AiError:
        return AiError(
            "no ai backend configured: set LEECH_AI_URL / LEECH_TTS_URL for "
            "manager mode, or GEMINI_API_KEY / GROQ_API_KEY for direct mode "
            f"(LEECH_AI_BACKEND={self._mode!r} was forced)."
        )

    async def chat(self, **_):
        raise self._fail()

    async def usage(self):
        return ["  (no backend configured)"], {}

    async def add_slot(self, *_, **__):
        raise self._fail()

    async def remove_slot(self, *_):
        raise self._fail()

    async def promote_slot(self, *_):
        raise self._fail()

    async def reset_chain(self):
        raise self._fail()

    def chain_state(self) -> Optional[list]:
        return None

    async def aclose(self) -> None:
        return None


class AiService:
    def __init__(
        self,
        *,
        manager_url: str = "",
        manager_token: str = "",
        thinking: str = "low",
        turn_deadline: float = 75.0,
        window_limit: int = 60,
        window_budget: int = 8_000,
        store: Store | None = None,
        backend: str = "",
    ):
        mode = (backend or os.getenv("LEECH_AI_BACKEND") or "auto").strip().lower()
        keys = _env_keys()
        if mode not in ("auto", "manager", "direct"):
            mode = "auto"
        if mode == "manager" or (mode == "auto" and manager_url):
            if manager_url:
                self._backend = _ManagerBackend(
                    ManagerAi(manager_url, manager_token, timeout=turn_deadline + 15.0)
                )
            else:
                self._backend = _MissingBackend(mode)
        elif mode == "direct" or (mode == "auto" and keys):
            self._backend = _DirectBackend(turn_deadline=turn_deadline, keys=keys)
        else:
            self._backend = _MissingBackend(mode)

        self._generation = Generation(thinking=thinking)
        self._window_limit = window_limit
        self._window_budget = window_budget
        self.personas: dict[int, str] = {}
        self.active: set[int] = set()
        self.muted: set[int] = set()
        self.windows: dict[int, ContextWindow] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # v10.9 AI_OPSEC_2: one resolution at a time per channel, and
        # ambient window writes are held back while it runs.
        self._resolving: set[int] = set()
        self._deferred: dict[int, list[tuple[str, int, str, Optional[str]]]] = {}
        self._resolution_locks: dict[int, asyncio.Lock] = {}
        self._store = store or get_store()
        # v11: ai config + personas ride the store. a failed load means
        # defaults this session and NO saves (never a wipe).
        self._store_broken = False
        self._load()
        if isinstance(self._backend, _DirectBackend):
            self._backend.set_persist_hook(self._persist)

    def service_name(self) -> str:
        return self._backend.name

    def persona(self, channel_id: int) -> str:
        return self.personas.get(channel_id, DEFAULT_PERSONA)

    def set_persona(self, channel_id: int, prompt: str) -> None:
        self.personas[channel_id] = prompt
        self._persist()

    def clear_persona(self, channel_id: int) -> None:
        self.personas.pop(channel_id, None)
        self._persist()

    def activate(self, channel_id: int) -> None:
        self.active.add(channel_id)
        self._persist()

    def deactivate(self, channel_id: int) -> None:
        self.active.discard(channel_id)
        self.windows.pop(channel_id, None)
        self._persist()

    def mute(self, channel_id: int) -> None:
        self.muted.add(channel_id)
        self.deactivate(channel_id)

    def unmute(self, channel_id: int) -> None:
        self.muted.discard(channel_id)
        self._persist()

    def window(self, channel_id: int) -> ContextWindow:
        return self.windows.setdefault(
            channel_id,
            ContextWindow(limit=self._window_limit, budget=self._window_budget),
        )

    def observe(
        self,
        channel_id: int,
        author: str,
        author_id: int,
        content: str,
        *,
        reply: Optional[str] = None,
    ) -> None:
        """v10.9 AI_OPSEC_2: an ambient window write (someone chatting).
        while a resolution runs in this channel the turn is held back and
        lands - in arrival order - once it finishes, so the model never
        sees other users' messages interleaved into the middle of the
        turn it is still resolving."""
        if channel_id in self._resolving:
            self._deferred.setdefault(channel_id, []).append(
                (author, author_id, content, reply)
            )
            return
        self.window(channel_id).push(author, author_id, content, reply=reply)

    async def begin_resolution(self, channel_id: int) -> bool:
        """v10.9: one request() resolution at a time per channel. False =
        one is already running; the caller deflects politely instead of
        interleaving a second one into the same window."""
        lock = self._resolution_locks.setdefault(channel_id, asyncio.Lock())
        if lock.locked():
            return False
        await lock.acquire()
        self._resolving.add(channel_id)
        return True

    def end_resolution(self, channel_id: int) -> None:
        self._resolving.discard(channel_id)
        pending = self._deferred.pop(channel_id, [])
        lock = self._resolution_locks.get(channel_id)
        if lock is not None and lock.locked():
            lock.release()
        window = self.window(channel_id)
        for author, author_id, content, reply in pending:
            window.push(author, author_id, content, reply=reply)

    def set_window_budget(self, chars: int) -> None:
        if chars < 1_000:
            raise ValueError("window budget must be at least 1000 chars.")
        self._window_budget = chars
        for window in self.windows.values():
            window.set_budget(chars)
        self._persist()

    async def usage(self) -> tuple[list[str], dict]:
        """chain lines for `>ai model list` / `>ai status`, plus daily usage."""
        return await self._backend.usage()

    async def add_slot(
        self, provider_name: str, model: str, position: Optional[int] = None
    ) -> str:
        return await self._backend.add_slot(provider_name, model, position)

    async def remove_slot(self, key: str) -> None:
        await self._backend.remove_slot(key)

    async def promote_slot(self, key: str) -> None:
        await self._backend.promote_slot(key)

    async def reset_chain(self) -> None:
        await self._backend.reset_chain()

    async def aclose(self) -> None:
        await self._backend.aclose()

    async def chat(
        self,
        channel_id: int,
        author: str,
        author_id: int,
        content: str,
        *,
        system: str,
        reply: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        lock = self._locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            viewable = [a for a in attachments if a.viewable]
            notes = [self._note(a) for a in attachments if not a.viewable]
            sendable: list[Attachment] = []
            total = 0
            for a in viewable:
                need = (len(a.data) + 2) // 3 * 4
                if total + need > TUNNEL_B64_MAX:
                    notes.append(
                        f"(image '{a.filename}' is too big for the tunnel. not shown)"
                    )
                    continue
                total += need
                sendable.append(a)
            if notes:
                content = f"{content}\n" + "\n".join(notes)

            window = self.window(channel_id)
            window.push(author, author_id, content, reply=reply)

            text, vision_used, slot = await self._backend.chat(
                system=system,
                turns=window.render(),
                attachments=sendable,
                generation=self._generation,
            )
            if sendable and not vision_used:
                window.push(
                    "(system)",
                    0,
                    "(the attached image could not be processed. answering "
                    "without seeing it)",
                )
            window.push("leech", 0, text, is_bot=True)
            _logger.debug("ai turn via %s (vision=%s)", slot, vision_used)
            return text

    @staticmethod
    def _note(a: Attachment) -> str:
        size = (
            f"{a.size / 1024:.0f} kb"
            if a.size < 1024 * 1024
            else f"{a.size / (1024 * 1024):.1f} mb"
        )
        return (
            f"(attachment: '{a.filename}', {a.content_type or 'unknown type'}, "
            f"{size}. not viewable by me)"
        )

    def _load(self) -> None:
        try:
            personas = self._store.items(NS_PERSONAS)
            config = self._store.get_doc(NS_CONFIG)
        except StoreError as e:
            self._store_broken = True
            _logger.error(
                "ai store unreadable (%s). running on defaults; changes "
                "will NOT be saved this session - fix the store, restart.",
                e,
            )
            return
        if not isinstance(config, dict):
            config = {}
        clean: dict[int, str] = {}
        for key, value in personas.items():
            try:
                clean[int(key)] = value
            except (TypeError, ValueError):
                _logger.warning("skipping malformed persona key %r", key)
        self.personas = clean
        self.active = {
            int(x) for x in config.get("active", []) if str(x).lstrip("-").isdigit()
        }
        self.muted = {
            int(x) for x in config.get("muted", []) if str(x).lstrip("-").isdigit()
        }
        if isinstance(config.get("window_budget"), int):
            self._window_budget = max(1_000, config["window_budget"])
        if isinstance(self._backend, _DirectBackend):
            self._backend.restore_chain(config.get("chain"))

    def _persist(self) -> None:
        if self._store_broken:
            _logger.error(
                "ai config NOT saved: the store failed to load this "
                "session. restart once the store is reachable; nothing was "
                "overwritten."
            )
            return
        payload: dict = {
            "active": sorted(self.active),
            "muted": sorted(self.muted),
            "window_budget": self._window_budget,
        }
        chain = self._backend.chain_state()
        if chain is not None:
            payload["chain"] = chain
        try:
            self._store.put_doc(NS_CONFIG, payload)
            wanted = {str(key) for key in self.personas}
            live = self._store.items(NS_PERSONAS)
            for key, prompt in self.personas.items():
                self._store.put(NS_PERSONAS, str(key), prompt)
            for key in live:
                if key not in wanted:
                    self._store.delete(NS_PERSONAS, key)
        except StoreError as e:
            _logger.error("ai config not saved: %s", e)
