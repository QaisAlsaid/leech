from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import discord
from discord import InteractionType
from discord.commands import _generate_nonce, _wrapped_interaction

from leech.core.helpers.interactions import Match, ResponseWaiter
from leech.core.helpers.interactions.model import (
    HarvestedComponent,
    HarvestedMessage,
    harvest_full,
)
from leech.core.helpers.interactions.waiter import stabilize
import logging

_logger = logging.getLogger(__name__)
from leech.core.helpers.interactions.waiter import _signature, stabilize
import time


@dataclass(frozen=True)
class PressTarget:
    channel_id: int
    application_id: int
    message_id: int
    component_type: int
    custom_id: str
    guild_id: Optional[int] = None

    @classmethod
    def of(
        cls, message: HarvestedMessage, component: HarvestedComponent
    ) -> "PressTarget":
        return cls(
            channel_id=message.channel_id,
            application_id=message.author_id,
            message_id=message.message_id,
            component_type=2 if component.kind == "button" else 3,
            custom_id=component.custom_id or "",
            guild_id=message.guild_id,
        )


async def _channel_of(client: discord.Client, channel_id: int):
    channel = client.get_channel(channel_id)
    if channel is None:
        channel = await client.fetch_channel(channel_id)
    return channel


async def press(
    client: discord.Client,
    target: PressTarget,
    *,
    message: Optional[discord.Message] = None,
) -> discord.Interaction:
    channel = await _channel_of(client, target.channel_id)
    resolved = await channel._get_channel()
    if message is None:
        message = await channel.fetch_message(target.message_id)
    data = {
        "component_type": target.component_type,
        "custom_id": target.custom_id,
    }
    return await _wrapped_interaction(
        client._connection,
        _generate_nonce(),
        InteractionType.component,
        target.custom_id,
        resolved,
        data,
        message=message,
    )


async def press_and_collect(
    client: discord.Client,
    target: PressTarget,
    *,
    message: Optional[discord.Message] = None,
    timeout: float = 12.0,
) -> Optional[HarvestedMessage]:
    channel = await _channel_of(client, target.channel_id)
    waiter = ResponseWaiter(client)
    match = Match(
        channel_id=target.channel_id,
        author_id=target.application_id,
        invoker_id=client.user.id,
    )

    if message is None:
        message = await channel.fetch_message(target.message_id)
    baseline = _signature(message)

    async def wait_edit() -> Optional[discord.Message]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.75)
            try:
                current = await channel.fetch_message(target.message_id)
            except discord.HTTPException:
                return None
            if _signature(current) != baseline:
                return current
        return None

    new_task = asyncio.ensure_future(waiter.wait(match, timeout=timeout))
    edit_task = asyncio.ensure_future(wait_edit())
    press_task = asyncio.ensure_future(press(client, target, message=message))
    try:
        await asyncio.wait_for(press_task, timeout=2.0)
    except asyncio.TimeoutError:
        pass
    except discord.HTTPException:
        for task in (new_task, edit_task, press_task):
            task.cancel()
        await asyncio.gather(new_task, edit_task, press_task, return_exceptions=True)
        raise
    except Exception:
        pass

    deadline = time.monotonic() + timeout
    result: Optional[discord.Message] = None
    while result is None:
        active = [task for task in (new_task, edit_task) if not task.done()]
        if not active:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        done, _ = await asyncio.wait(
            set(active), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            try:
                value = task.result()
            except (
                asyncio.TimeoutError,
                asyncio.CancelledError,
                discord.HTTPException,
            ):
                continue
            if value is not None:
                result = value
                break

    for task in (new_task, edit_task, press_task):
        if not task.done():
            task.cancel()
    await asyncio.gather(new_task, edit_task, press_task, return_exceptions=True)

    if result is None:
        return None
    result = await stabilize(result, client=client, channel=channel)
    return await harvest_full(result, client)
