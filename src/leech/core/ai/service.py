from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

from leech.core.error import BaseError


class AiError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


DEFAULT_PERSONA = (
    "you are leech, a dry, low key, assistant living inside a discord bot, chatting "
    "with a couple of humans. keep replies short and conversational, plain text. "
    "you may run the bot's commands on request. never claim an action happened "
    "unless you emitted the command for it."
)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class ContextWindow:
    def __init__(self, limit: int = 40, max_chars: int = 600):
        self._messages: deque = deque(maxlen=limit)
        self._max_chars = max_chars

    def push(self, author: str, content: str, *, is_bot: bool = False) -> None:
        content = content.strip()[: self._max_chars]
        if not content:
            return
        self._messages.append((author, content, is_bot))

    def render(self) -> list[dict]:
        return [
            {
                "role": "model" if is_bot else "user",
                "parts": [{"text": content if is_bot else f"{author}: {content}"}],
            }
            for author, content, is_bot in self._messages
        ]

    def clear(self) -> None:
        self._messages.clear()

    def size(self) -> int:
        return len(self._messages)


class AiService:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        *,
        timeout: float = 40.0,
        store: Path = Path("persistent/v1/ai/ai.json"),
    ):
        self.model = model
        self._key = api_key
        self._http = httpx.AsyncClient(timeout=timeout)
        self._store = store
        self.personas: dict[int, str] = {}
        self.active: set[int] = set()
        self.windows: dict[int, ContextWindow] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._load()

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
        self.windows.pop(channel_id, None)  # off means forget, not mute
        self._persist()

    def window(self, channel_id: int) -> ContextWindow:
        return self.windows.setdefault(channel_id, ContextWindow())

    async def chat(
        self, channel_id: int, author: str, content: str, *, system: str
    ) -> str:
        lock = self._locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            window = self.window(channel_id)
            window.push(author, content)
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": window.render(),
                "generationConfig": {
                    "response_mime_type": "application/json",
                    "maxOutputTokens": 700,
                    "temperature": 0.6,
                },
            }
            response = await self._post(body)
            if response.status_code == 429:
                await asyncio.sleep(2.5)
                response = await self._post(body)
            if response.status_code >= 400:
                raise AiError(f"the model endpoint answered: {response.status_code}.")
            try:
                parts = response.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
            except (KeyError, IndexError):
                raise AiError(
                    "the model returned nothing (filtered or empty)."
                ) from None
            if not text:
                raise AiError("the model returned nothing (filtered or empty).")
            window.push("leech", text, is_bot=True)
            return text

    async def _post(self, body: dict) -> httpx.Response:
        return await self._http.post(
            API.format(model=self.model), params={"key": self._key}, json=body
        )

    def _load(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        if not self._store.exists():
            return
        data = json.loads(self._store.read_text(encoding="utf-8"))
        self.personas = {int(k): v for k, v in data.get("personas", {}).items()}
        self.active = set(data.get("active", []))

    def _persist(self) -> None:
        tmp = self._store.with_suffix(".json.temp")
        tmp.write_text(
            json.dumps(
                {
                    "personas": {str(k): v for k, v in self.personas.items()},
                    "active": sorted(self.active),
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._store)
