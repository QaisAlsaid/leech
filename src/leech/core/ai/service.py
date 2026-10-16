from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence
import base64

import httpx

from leech.core.error import BaseError

_logger = logging.getLogger(__name__)


class AiError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


DEFAULT_PERSONA = (
    "you are leech, a dry but helpful, low key, assistant living inside a discord bot, chatting "
    "with a couple of humans. keep replies short and conversational, plain text. "
    "you may run the bot's commands on request."
)

GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GROQ_API = "https://api.groq.com/openai/v1"

THINKING_EFFORT = {"low": "low", "high": "high"}


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


class Provider(ABC):
    name: str = "?"
    vision: bool = False

    def __init__(self, api_key: str, *, read_timeout: float = 20.0):
        self._key = api_key
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(read_timeout, connect=5.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    @abstractmethod
    def _request(
        self,
        model: str,
        system: str,
        turns: list[tuple[bool, str]],
        generation: Generation,
    ) -> tuple[str, dict, dict]:
        pass

    @abstractmethod
    def _extract(self, data: dict) -> str:
        pass

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


class AiService:
    def __init__(
        self,
        chain: list[tuple[str, str]],
        keys: dict[str, str],
        *,
        thinking: str = "low",
        read_timeout: float = 20.0,
        turn_deadline: float = 75.0,
        cooldown: float = 90.0,
        window_limit: int = 60,
        window_budget: int = 8_000,
        store: Path = Path("persistent/v1/ai/ai.json"),
    ):
        self._keys = keys
        self._default_chain = [tuple(e) for e in chain]
        self._read_timeout = read_timeout
        self._generation = Generation(thinking=thinking)
        self._deadline = turn_deadline
        self._cooldown = cooldown
        self._window_limit = window_limit
        self._window_budget = window_budget
        self._cooldowns: dict[str, float] = {}
        self.personas: dict[int, str] = {}
        self.active: set[int] = set()
        self.windows: dict[int, ContextWindow] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._slots: list[Slot] = []
        self._store = store
        self._load()

    def chain(self) -> list[tuple[str, str]]:
        return [(s.provider.name, s.model) for s in self._slots]

    def _validate_entry(self, provider_name: str, model: str) -> None:
        if not model:
            raise ValueError(
                "missing model. use provider:model, e.g. groq:openai/gpt-oss-120b."
            )
        if provider_name not in PROVIDERS:
            raise ValueError(
                f"unknown provider '{provider_name}' (known: {', '.join(PROVIDERS)})."
            )
        if provider_name not in self._keys:
            raise ValueError(f"no api key configured for provider '{provider_name}'.")

    def _create_slot(self, provider_name: str, model: str) -> Slot:
        self._validate_entry(provider_name, model)
        provider = PROVIDERS[provider_name](
            self._keys[provider_name], read_timeout=self._read_timeout
        )
        return Slot(provider=provider, model=model)

    async def _rebuild(self, chain: list[tuple[str, str]]) -> None:
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

    async def add_slot(
        self, provider_name: str, model: str, position: Optional[int] = None
    ) -> Slot:
        self._validate_entry(provider_name, model)
        key = f"{provider_name}:{model}"
        if any(s.key == key for s in self._slots):
            raise ValueError(f"'{key}' is already in the chain.")
        chain = self.chain()
        entry = (provider_name, model)
        if position is None:
            chain.append(entry)
        else:
            chain.insert(max(0, min(position, len(chain))), entry)
        await self._rebuild(chain)
        self._persist()
        return next(s for s in self._slots if s.key == key)

    async def remove_slot(self, key: str) -> None:
        chain = self.chain()
        remaining = [e for e in chain if f"{e[0]}:{e[1]}" != key]
        if len(remaining) == len(chain):
            raise ValueError(f"'{key}' is not in the chain.")
        if not remaining:
            raise ValueError("can't remove the last model in the chain.")
        await self._rebuild(remaining)
        self._persist()

    async def promote_slot(self, key: str) -> None:
        chain = self.chain()
        entry = next((e for e in chain if f"{e[0]}:{e[1]}" == key), None)
        if entry is None:
            raise ValueError(f"'{key}' is not in the chain.")
        chain.remove(entry)
        chain.insert(0, entry)
        await self._rebuild(chain)
        self._persist()

    async def reset_chain(self) -> None:
        await self._rebuild([tuple(e) for e in self._default_chain])
        self._persist()

    async def aclose(self) -> None:
        for slot in self._slots:
            await slot.provider.aclose()

    def set_window_budget(self, chars: int) -> None:
        if chars < 1_000:
            raise ValueError("window budget must be at least 1000 chars.")
        self._window_budget = chars
        for window in self.windows.values():
            window.set_budget(chars)
        self._persist()

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

    def window(self, channel_id: int) -> ContextWindow:
        return self.windows.setdefault(
            channel_id,
            ContextWindow(limit=self._window_limit, budget=self._window_budget),
        )

    def usage(self) -> list[str]:
        now = time.monotonic()
        lines = []
        for index, slot in enumerate(self._slots):
            until = self._cooldowns.get(slot.key, 0.0)
            state = f"cooling ({until - now:.0f}s left)" if now < until else "ready"
            mark = " (main)" if index == 0 else ""
            lines.append(f"`{slot.key}`{mark}: {state}")
        return lines

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
            ready_vision: list[Slot] = []
            if viewable:
                now = time.monotonic()
                ready_vision = [
                    s
                    for s in self._slots
                    if s.provider.vision and now >= self._cooldowns.get(s.key, 0.0)
                ]
                if not ready_vision:
                    notes += [
                        f"(image '{a.filename}'. no vision model ready. not shown)"
                        for a in viewable
                    ]
                    viewable = []
            if notes:
                content = f"{content}\n" + "\n".join(notes)

            window = self.window(channel_id)
            window.push(author, author_id, content, reply=reply)

            if viewable and ready_vision:
                try:
                    return await self._attempt(
                        ready_vision, window.render(), viewable, system, window
                    )
                except AiError as e:
                    _logger.warning(
                        "vision slots failed (%s). degrading to text only", e.message
                    )
                    window.push(
                        "(system)",
                        0,
                        "(the attached image could not be processed. answering "
                        "without seeing it)",
                    )
            return await self._attempt(
                list(self._slots), window.render(), [], system, window
            )

    async def _attempt(
        self,
        slots: list[Slot],
        turns: list[tuple[bool, str]],
        attachments: Sequence[Attachment],
        system: str,
        window: ContextWindow,
    ) -> str:
        failures: list[str] = []
        for slot in slots:
            if time.monotonic() < self._cooldowns.get(slot.key, 0.0):
                failures.append(f"{slot.key}: cooling")
                continue
            try:
                text = await asyncio.wait_for(
                    slot.provider.complete(
                        slot.model,
                        system=system,
                        turns=turns,
                        generation=self._generation,
                        attachments=attachments,
                    ),
                    timeout=self._deadline,
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
                failures.append(
                    f"{slot.key}: exceeded {self._deadline:.0f}s turn deadline"
                )
                continue
            window.push("leech", 0, text, is_bot=True)
            return text
        raise AiError("every model failed. " + "; ".join(failures) + ".")

    @staticmethod
    def _note(a: Attachment) -> str:
        size = (
            f"{a.size / 1024:.0f} kb"
            if a.size < 1024 * 1024
            else f"{a.size / (1024 * 1024):.1f} mb"
        )
        return f"(attachment: '{a.filename}', {a.content_type or 'unknown type'}, {size}. not viewable by me)"

    def _load(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        if not self._store.exists():
            self._slots = [self._create_slot(p, m) for p, m in self._default_chain]
            return
        data = json.loads(self._store.read_text(encoding="utf-8"))
        self.personas = {int(k): v for k, v in data.get("personas", {}).items()}
        self.active = set(data.get("active", []))
        if isinstance(data.get("window_budget"), int):
            self._window_budget = max(1_000, data["window_budget"])

        cleaned: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry in data.get("chain") or []:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            provider_name, model = entry
            key = f"{provider_name}:{model}"
            if (
                provider_name in PROVIDERS
                and provider_name in self._keys
                and model
                and key not in seen
            ):
                cleaned.append((provider_name, model))
                seen.add(key)
        if not cleaned:
            cleaned = [tuple(e) for e in self._default_chain]
        self._slots = [self._create_slot(p, m) for p, m in cleaned]

    def _persist(self) -> None:
        tmp = self._store.with_suffix(".json.temp")
        tmp.write_text(
            json.dumps(
                {
                    "personas": {str(k): v for k, v in self.personas.items()},
                    "active": sorted(self.active),
                    "chain": [[s.provider.name, s.model] for s in self._slots],
                    "window_budget": self._window_budget,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._store)
