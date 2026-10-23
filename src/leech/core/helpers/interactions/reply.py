"""v10.7 ReplySession - consume a user's replies BEFORE the AI sees them.

the bug this fixes: the paginator used `client.wait_for("message")`,
which is a passive tap - it never stops propagation. so a paging
reply ("next", "2") still fell through the cog manager's priority
queue to the ai cog's LOW listener: the AI answered it AND its window
filled up with page numbers. same story for any future "reply to my
question" flow (todo due-date prompts).

the fix: a session registers itself as a HIGH priority on_message
listener on the cog manager (auth gate stays FIRST, commands still
resolve before listeners ever run). when a message matches, the
session returns consumed=True and nothing lower - the AI included -
ever hears about it.

matching rules, tuned so sessions never eat unrelated chat:
- same channel, same (human) author, content matches the session's
  regex.
- a message that *replies* (discord reference) to something only
  matches when it references the session's own target message - so a
  "2" answering a button-press wizard or another bot message is left
  alone.
- a *bare* message (no reference) only matches while this session is
  the only open one for that (channel, author) - two open prompts
  answering the same bare text would be a coin flip.
- sessions die on close(); a caller-side timeout always applies.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

_logger = logging.getLogger(__name__)

_CLOSED = object()  # sentinel pushed on close()

# (channel_id, author_id) -> list of live sessions
_open: dict[tuple[int, int], list["ReplySession"]] = {}


class ReplySession:
    def __init__(
        self,
        client,
        channel_id: int,
        author_id: int,
        *,
        regex: re.Pattern,
        target_id: int | None = None,
        allow_bare: bool | None = None,
        name: str = "reply-session",
    ):
        """`regex` - a compiled, anchored pattern; a message must match
        it to even be considered. `target_id` - the bot message the user
        is expected to reply to. `allow_bare` - None: bare (reference-
        less) messages match only while this is the sole open session
        for (channel, author); True/False forces it."""
        self.client = client
        self.channel_id = channel_id
        self.author_id = author_id
        self.regex = regex
        self.target_id = target_id
        self.allow_bare = allow_bare
        self.name = name
        self.closed = False
        self.consumed = 0
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        # imported here, not at module top: leech.core.cog.context pulls
        # the whole cog stack, and a helper that drags it in refuses to
        # load standalone (circular import through leech.leech).
        from leech.core.cog.context import Priority

        self._dispose = client.cogs.add_on_message(Priority.HIGH, self._on_message)
        _open.setdefault((channel_id, author_id), []).append(self)
        _logger.debug(
            f"reply-session '{self.name}' open: channel={channel_id} "
            f"author={author_id} target={target_id}"
        )

    # ---------------------------------------------------------- matching
    def _is_solo(self) -> bool:
        return len(_open.get((self.channel_id, self.author_id), ())) <= 1

    def matches(self, message) -> bool:
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return False
        if getattr(author, "id", None) != self.author_id:
            return False
        channel = getattr(message, "channel", None)
        if getattr(channel, "id", None) != self.channel_id:
            return False
        if self.closed:
            return False
        content = message.content or ""
        if not self.regex.search(content):
            return False
        ref = getattr(message, "reference", None)
        ref_id = getattr(ref, "message_id", None) if ref is not None else None
        if ref_id is not None:
            if self.target_id is None or ref_id != self.target_id:
                return False  # replying to something that isn't ours
        else:
            bare = self.allow_bare if self.allow_bare is not None else self._is_solo()
            if not bare:
                return False
        return True

    async def _on_message(self, message):
        if not self.matches(message):
            return False, None
        self.consumed += 1
        self._queue.put_nowait(message)
        return True, None  # consumed: nothing below HIGH sees this

    # ---------------------------------------------------------- reading
    async def next(self, timeout: float | None = None):
        """await the next matching message. None on close or timeout."""
        if not self._queue.empty():
            item = self._queue.get_nowait()
            return None if item is _CLOSED else item
        try:
            if timeout is None:
                item = await self._queue.get()
            else:
                item = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
        return None if item is _CLOSED else item

    # ---------------------------------------------------------- closing
    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._dispose()
        except Exception:
            _logger.exception("reply-session dispose failed")
        sessions = _open.get((self.channel_id, self.author_id), [])
        if self in sessions:
            sessions.remove(self)
        if not sessions:
            _open.pop((self.channel_id, self.author_id), None)
        self._queue.put_nowait(_CLOSED)
        _logger.debug(
            f"reply-session '{self.name}' closed: channel={self.channel_id} "
            f"author={self.author_id} consumed={self.consumed}"
        )


def close_others(channel_id: int, author_id: int, keep: "ReplySession") -> None:
    """shut every other session for (channel, author) - e.g. a fresh
    paginator supersedes an old one."""
    for session in list(_open.get((channel_id, author_id), [])):
        if session is not keep:
            session.close()


def open_sessions() -> int:
    return sum(len(sessions) for sessions in _open.values())
