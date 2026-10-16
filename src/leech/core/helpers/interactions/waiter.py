from __future__ import annotations

import asyncio

import discord

from .match import Match
import time
import logging

_logger = logging.getLogger(__name__)


def _signature(message: discord.Message) -> str:
    embeds = ";".join(
        f"{e.title}|{e.description}|{getattr(e.image, 'url', None)}"
        for e in message.embeds
    )
    attachments = ";".join(a.url for a in message.attachments)
    return f"{message.content}|{embeds}|{attachments}|{len(message.components or [])}"


def _is_placeholder(message: discord.Message) -> bool:
    if bool(getattr(message.flags, "value", 0) & (1 << 15)):
        return False  # v2: payload invisible to the parser
    return not (
        message.content or message.embeds or message.attachments or message.components
    )


async def _edit_event(client, message_id: int, timeout: float) -> bool:
    """wake-up shortcut only — event delivery is never assumed."""
    try:
        await asyncio.wait_for(
            client.wait_for(
                "raw_message_edit", check=lambda event: event.message_id == message_id
            ),
            timeout=timeout,
        )
        return True
    except asyncio.TimeoutError:
        return False


async def stabilize(
    message: discord.Message,
    *,
    client,
    channel,
    pending_wait: float = 60.0,
    poll: float = 2.0,
) -> discord.Message:
    if bool(getattr(message.flags, "ephemeral", False)):
        return message

    last = message
    deadline = (
        time.monotonic() + pending_wait
        if _is_placeholder(last)
        else time.monotonic() + 2 * poll + 1.0
    )

    while time.monotonic() < deadline:
        try:
            current = await channel.fetch_message(message.id)
        except discord.HTTPException:
            return last
        if not _is_placeholder(current):
            if _signature(current) == _signature(last):
                return current
            if _is_placeholder(last):
                deadline = time.monotonic() + 2 * poll + 1.0
        last = current
        _logger.debug(
            "stabilize: %s embeds=%d attachments=%d",
            message.id,
            len(current.embeds),
            len(current.attachments),
        )
        await _edit_event(client, message.id, poll)
    return last


class InteractionTimeout(Exception):
    pass


class ResponseWaiter:
    def __init__(self, client: discord.Client):
        self._client = client

    def wait(self, match: Match, *, timeout: float):
        return self._client.wait_for("message", check=match.check, timeout=timeout)

    async def _sweep(
        self, match: Match, channel, marker_id: int, scan: int
    ) -> discord.Message | None:
        try:
            after = discord.Object(id=marker_id)
            async for message in channel.history(
                limit=scan, after=after, oldest_first=True
            ):
                if match.check(message):
                    return message
        except discord.HTTPException:
            pass
        return None

    async def wait_or_reconcile(
        self, match: Match, *, channel, marker_id: int, timeout: float, scan: int = 25
    ) -> discord.Message:
        found = await self._sweep(match, channel, marker_id, scan)
        if found is not None:
            return found
        try:
            return await self.wait(match, timeout=timeout)
        except asyncio.TimeoutError:
            pass
        found = await self._sweep(match, channel, marker_id, scan)
        if found is not None:
            return found
        raise InteractionTimeout(f"no matching response within {timeout}s")
