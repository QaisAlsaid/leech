import asyncio
import contextlib
import importlib
import logging
from typing import Callable, Coroutine

from .cog import Cog
from .context import CogContext, Priority
from .error import CogError

_logger = logging.getLogger(__name__)


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

    def add_listener(self, priority: Priority, cb: Callable) -> Callable[[], None]:
        match priority:
            case Priority.FIRST:
                if self._first:
                    raise ValueError("first is already set.")
                self._first = cb

                def dispose():
                    if self._first is cb:
                        self._first = None

            case Priority.HIGH:
                self._high.append(cb)

                def dispose():
                    with contextlib.suppress(ValueError):
                        self._high.remove(cb)

            case Priority.MEDIUM:
                self._medium.append(cb)

                def dispose():
                    with contextlib.suppress(ValueError):
                        self._medium.remove(cb)

            case Priority.LOW:
                self._low.append(cb)

                def dispose():
                    with contextlib.suppress(ValueError):
                        self._low.remove(cb)

            case Priority.LAST:
                if self._last:
                    raise ValueError("last is already set.")
                self._last = cb

                def dispose():
                    if self._last is cb:
                        self._last = None

            case _:
                raise ValueError("invalid priority.")
        return dispose


class CogManager:
    def __init__(self, client):
        self.client = client
        self._on_message: PrioritizedQueues = PrioritizedQueues()
        self.loaded: dict[str, Cog] = {}
        self._tasks: dict[str, set[asyncio.Task]] = {}

    def spawn(
        self, owner: str, coro: Coroutine, *, name: str | None = None
    ) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        bucket = self._tasks.setdefault(owner, set())
        bucket.add(task)
        task.add_done_callback(bucket.discard)
        return task

    async def reap(self, owner: str) -> None:
        tasks = self._tasks.pop(owner, set())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def add_on_message(self, priority: Priority, cb: Callable) -> Callable[[], None]:
        return self._on_message.add_listener(priority, cb)

    async def load(self, path: str):
        if path in self.loaded:
            raise CogError(f"cog: {path}, is already loaded")
        cog: Cog | None = None
        try:
            module = importlib.import_module(path)
            cog = module.create_cog(CogContext(client=self.client, owner=path))
            await cog.context.load()
        except Exception as e:
            if cog is not None:
                # roll back partially-registered commands/listeners from a failed load
                try:
                    await cog.context.unload()
                except Exception:
                    _logger.exception(f"rollback failed while loading cog: '{path}'")
            if isinstance(e, CogError):
                raise
            raise CogError(f"failed to load cog: '{path}', details: {e}") from e
        self.loaded[path] = cog
        _logger.debug(f"loaded cog: '{path}'")

    async def unload(self, path: str) -> bool:
        cog = self.loaded.get(path)
        if cog is None:
            _logger.error(f"failed to unload cog: '{path}'")
            return False

        await cog.context.unload()
        await self.reap(path)
        self.loaded.pop(path)
        _logger.debug(f"unloaded cog: '{path}'")
        return True

    async def reload(self, path: str) -> bool:
        if path not in self.loaded:
            _logger.error(f"failed to reload cog: '{path}'")
            return False
        module = importlib.import_module(path)
        await self.unload(path)
        importlib.reload(module)
        await self.load(path)
        return True

    async def on_message(self, message):
        return await self._on_message.on_message(message)
