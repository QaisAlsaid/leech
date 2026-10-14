import asyncio
import logging
from typing import Callable, Coroutine

import leech.leech as leech
from leech.core.command.registry import CommandRegistry, Command

from enum import Enum, auto

_logger = logging.getLogger(__name__)


class Priority(Enum):
    FIRST = auto()
    HIGH = auto()
    MEDIUM = auto()
    LOW = auto()
    LAST = auto()


class CogContext:
    def __init__(self, client, owner: str):
        self.client: leech.Leech = client
        self.owner: str = owner
        self._cleanup_commands: list[str] = []
        self._cleanup_listeners: list[Callable[[], None]] = []
        self._on_load: list[Callable] = []
        self._on_unload: list[Callable] = []

    async def load(self):
        for cb in self._on_load:
            await cb()

    async def unload(self):
        try:
            for cb in self._on_unload:
                await cb()
        finally:
            # cleanup must run even if an unload callback blew up —
            # otherwise commands/listeners leak on every failed unload
            await self._cleanup()

    def add_on_message(self, priority: Priority, cb: Callable) -> Callable[[], None]:
        dispose = self.client.cogs.add_on_message(priority, cb)
        self._cleanup_listeners.append(dispose)
        return dispose

    def spawn(self, coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
        """tracked task — cancelled when this cog unloads/reloads."""
        return self.client.cogs.spawn(self.owner, coro, name=name)

    def add_on_load(self, cb: Callable):
        self._on_load.append(cb)

    def add_on_unload(self, cb: Callable):
        self._on_unload.append(cb)

    def add_command(self, path: str, command: Command) -> bool:
        if self.client.commands.register_command(path, command):
            self._cleanup_commands.append(path)
            return True
        return False

    async def _cleanup(self):
        for dispose in reversed(self._cleanup_listeners):
            try:
                dispose()
            except Exception:
                _logger.exception("listener disposal failed")
        self._cleanup_listeners.clear()

        for path in reversed(self._cleanup_commands):
            self.client.commands.unregister_command(path)
        self._cleanup_commands.clear()
