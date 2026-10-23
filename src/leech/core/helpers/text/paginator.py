from __future__ import annotations

import logging
import re
import time

import discord

import leech.core.helpers.text.splitter as splitter
from leech.core.helpers.interactions.reply import ReplySession, close_others

_logger = logging.getLogger(__name__)

PAGE_SIZE = 1900
DEFAULT_TIMEOUT = 300.0
PAGE_REPLY_RE = re.compile(
    r"^\s*(?:(next|n|prev|p|back|b|stop|cancel|end|first|last)|(\d{1,4}))\s*$",
    re.IGNORECASE,
)


def _footer(index: int, total: int, closing: str = "") -> str:
    hint = "reply: next / prev / <number> / stop"
    tail = f" · {closing}" if closing else ""
    return f"\n\n`page {index}/{total} · {hint}{tail}`"


# v10.9: the footer is navigation chrome for humans. consumers that feed
# bot messages back into other systems (the ai's command-output harvest)
# strip it with this instead of reading it as data.
FOOTER_RE = re.compile(r"\s*`page \d+/\d+ · reply: [^`]*`\s*", re.IGNORECASE)


def strip_footer(text: str) -> str:
    return FOOTER_RE.sub("", text or "").strip()


def _pages_of(text: str) -> list[str]:
    return splitter.split(text, PAGE_SIZE)


def _match_page_command(content: str) -> tuple[str, int | None]:
    number = re.match(r"^\s*(\d{1,4})\s*$", content)
    if number:
        return "goto", int(number.group(1))
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


async def _set_page(
    message: discord.Message, pages: list[str], index: int, closing: str = ""
) -> None:
    try:
        await message.edit(
            content=pages[index] + _footer(index + 1, len(pages), closing)
        )
    except discord.HTTPException:
        pass


async def paginate(
    message: discord.Message,
    text: str,
    *,
    client,
    timeout: float = DEFAULT_TIMEOUT,
    author_id: int | None = None,
) -> None:
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
        for chunk in pages:
            try:
                await message.reply(chunk, mention_author=False)
            except discord.HTTPException:
                pass
        return

    index = 0
    session = ReplySession(
        client,
        channel_id,
        who,
        regex=PAGE_REPLY_RE,
        target_id=current.id,
        name="paginator",
    )
    close_others(channel_id, who, session)

    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await _set_page(current, pages, index, closing="closed")
                return
            reply = await session.next(timeout=remaining)
            if reply is None:
                await _set_page(current, pages, index, closing="closed")
                return
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
            deadline = time.monotonic() + timeout
            await _set_page(current, pages, index)
    finally:
        session.close()
