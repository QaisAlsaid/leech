"""v10.6 global paginator - one stateful session for any long output.

the problem: `>todo list` on a 700-task list (or >help) splits into N
messages that all fire at once - a wall of text and a rate-limit risk.

the fix: send the FIRST page, then passively listen for the invoker's
replies in the same channel (`next` / `prev` / a number / `stop`) and
edit the message in place. the session dies on timeout, on `stop`, or
when the cog unloads.

why this shape:
  - `client.wait_for` is a passive listener - it does NOT touch the
    dispatcher, the command chain, or any on_message hook, so commands
    keep working while a paginator is open. paging replies ("next")
    are plain text and never look like commands.
  - the full text still goes to `invocation.chain.out` (callers already
    do that) so AI/chain consumers see everything, not just page 1.
  - pages are built with the existing splitter, so any text that used
    to `for chunk in splitter.split(...)` can switch to one
    `await paginator.paginate(...)` call.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import discord

import leech.core.helpers.text.splitter as splitter

_logger = logging.getLogger(__name__)

PAGE_SIZE = 1900  # room for the footer inside the 2000 limit
DEFAULT_TIMEOUT = 300.0
PAGE_COMMANDS = re.compile(
    r"^\s*(next|n|prev|p|back|b|stop|cancel|end|first|last)\s*$", re.IGNORECASE
)
PAGE_NUMBER = re.compile(r"^\s*(\d{1,4})\s*$")


def _footer(index: int, total: int, closing: str = "") -> str:
    hint = "reply: next / prev / <number> / stop"
    tail = f" · {closing}" if closing else ""
    return f"\n\n`page {index}/{total} · {hint}{tail}`"


def _pages_of(text: str) -> list[str]:
    return splitter.split(text, PAGE_SIZE)


def _match_page_command(content: str) -> tuple[str, int | None]:
    """-> (kind, page?) where kind in {next, prev, first, last, stop, goto}."""
    if PAGE_NUMBER.match(content):
        return "goto", int(content.strip())
    word = content.strip().lower()
    if word in ("next", "n"):
        return "next", None
    if word in ("prev", "p", "back", "b"):
        return "prev", None
    if word in ("stop", "cancel", "end"):
        return "stop", None
    if word == "first":
        return "first", None
    if word == "last":
        return "last", None
    return "", None


async def _set_page(message: discord.Message, pages: list[str], index: int,
                    closing: str = "") -> None:
    try:
        await message.edit(
            content=pages[index] + _footer(index + 1, len(pages), closing)
        )
    except discord.HTTPException:
        pass  # deleted/locked message - the session dies on next timeout


async def paginate(
    message: discord.Message,
    text: str,
    *,
    client,
    timeout: float = DEFAULT_TIMEOUT,
    author_id: int | None = None,
) -> None:
    """render `text` as an interactive paged reply to `message`.

    short texts (one page) are sent plainly - no session, no footer.
    the invoker (message.author, or author_id when the cog knows better)
    is the only one allowed to flip pages. errors never propagate into
    the caller's command flow; on failure we degrade to plain splitting.
    """
    pages = _pages_of(text)
    if len(pages) <= 1:
        try:
            await message.reply(text[:1990], mention_author=False)
        except discord.HTTPException:
            _logger.warning("paginator: direct reply failed")
        return

    who = author_id if author_id is not None else message.author.id
    channel_id = message.channel.id
    try:
        current = await message.reply(
            pages[0] + _footer(1, len(pages)), mention_author=False
        )
    except discord.HTTPException:
        for chunk in pages:  # degrade to the old behavior
            try:
                await message.reply(chunk, mention_author=False)
            except discord.HTTPException:
                pass
        return

    index = 0
    last_seen = current.id

    def _is_paging_reply(candidate: discord.Message) -> bool:
        return (
            candidate.channel.id == channel_id
            and candidate.author.id == who
            and candidate.id > last_seen
            and bool(
                PAGE_COMMANDS.match(candidate.content or "")
                or PAGE_NUMBER.match(candidate.content or "")
            )
        )

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _set_page(current, pages, index, closing="closed")
            return
        try:
            reply = await asyncio.wait_for(
                client.wait_for("message", check=_is_paging_reply),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            await _set_page(current, pages, index, closing="closed")
            return
        last_seen = reply.id
        kind, target = _match_page_command(reply.content or "")
        total = len(pages)
        if kind == "stop":
            await _set_page(current, pages, index, closing="stopped")
            return
        if kind == "next":
            index = min(index + 1, total - 1)
        elif kind == "prev":
            index = max(index - 1, 0)
        elif kind == "first":
            index = 0
        elif kind == "last":
            index = total - 1
        elif kind == "goto":
            index = max(0, min((target or 1) - 1, total - 1))
        else:
            continue
        # fresh lease on activity so a slow reader doesn't get yanked away
        deadline = time.monotonic() + timeout
        await _set_page(current, pages, index)
