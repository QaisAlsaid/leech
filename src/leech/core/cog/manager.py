import importlib
from enum import Enum, auto
from typing import Callable
from .cog import Cog
from .context import CogContext, Priority
from .error import CogError
import logging

_logger = logging.getLogger(__file__)


class PrioritizedQueues:
    def __init__(self):
        self._first: Callable | None = None
        self._last: Callable | None = None
        self._high: list[Callable] = []
        self._medium: list[Callable] = []
        self._low: list[Callable] = []

    async def on_message(self, message):
        if self._first:
            consumed, invk = await self._first(message)
            if consumed:
                return True, invk
        for cb in reversed(self._high):
            consumed, invk = await cb(message)
            if consumed:
                return True, invk
        for cb in reversed(self._medium):
            consumed, invk = await cb(message)
            if consumed:
                return True, invk
        for cb in reversed(self._low):
            consumed, invk = await cb(message)
            if consumed:
                return True, invk

        if self._last:
            consumed, invk = await self._last(message)
            if consumed:
                return True, invk

        return False, None

    def add_listener(self, priority: Priority, cb: Callable):
        match priority:
            case Priority.FIRST:
                if self._first:
                    raise ValueError("first is already set.")
                self._first = cb
            case Priority.HIGH:
                self._high.append(cb)
            case Priority.MEDIUM:
                self._medium.append(cb)
            case Priority.LOW:
                self._low.append(cb)
            case Priority.LAST:
                if self._last:
                    raise ValueError("last is already set.")
                self._last = cb
            case _:
                raise ValueError("invalid priority.")


class CogManager:
    def __init__(self, client):
        self.client = client
        self._on_message: PrioritizedQueues = PrioritizedQueues()
        self.loaded: dict[str, Cog] = {}

    async def load(self, path: str):
        if path in self.loaded:
            raise CogError(f"cog: {path}, is already loaded")
        try:
            module = importlib.import_module(path)
        except Exception as e:
            raise CogError(f"failed to import module: '{path}', details: {e}")
        cog: Cog = module.create_cog(CogContext(client=self.client))
        await cog.context.load()
        self.loaded[path] = cog
        _logger.debug(f"loaded cog: '{path}'")

    async def unload(self, path: str) -> bool:
        cog = self.loaded.get(path)
        if cog is None:
            _logger.error(f"failed to unload cog: '{path}'")
            return False

        await cog.context.unload()
        self.loaded.pop(path)
        _logger.debug(f"unloaded cog: '{path}'")
        return True

    async def reload(self, path: str):
        pass

    def add_on_message(self, priority: Priority, cb: Callable):
        self._on_message.add_listener(priority, cb)

    async def on_message(self, message):
        return await self._on_message.on_message(message)
