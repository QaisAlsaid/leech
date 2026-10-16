from __future__ import annotations

from typing import Optional

import discord

from leech.core.command.chain import Execution
from leech.core.helpers.interactions import (
    InteractionTimeout,
    Match,
    ResponseWaiter,
    stabilize,
)
from leech.core.helpers.interactions.model import HarvestedMessage, harvest_full
from . import invoker, resolver
import logging

_UNSET = object()

_logger = logging.getLogger(__name__)


def _is_blank(harvested: Optional[HarvestedMessage]) -> bool:
    return harvested is None or not (
        harvested.content
        or harvested.embeds
        or harvested.attachments
        or harvested.components
        or harvested.v2_media
    )


def convert_arguments(
    client: discord.Client,
    command,
    channel,
    arguments: dict,
) -> dict:
    converted = dict(arguments)
    for option in command.options:
        if option.name not in converted:
            continue
        value = converted[option.name]
        if not isinstance(value, int):
            continue
        match option.type:
            case (
                discord.ApplicationCommandOptionType.user
                | discord.ApplicationCommandOptionType.mentionable
            ):
                converted[option.name] = client.get_user(value) or discord.Object(
                    id=value
                )
            case discord.ApplicationCommandOptionType.channel:
                converted[option.name] = client.get_channel(value) or discord.Object(
                    id=value
                )
            case discord.ApplicationCommandOptionType.role:
                guild = getattr(channel, "guild", None)
                role = guild.get_role(value) if guild is not None else None
                converted[option.name] = role or discord.Object(id=value)
    return converted


async def execute_slash(
    client: discord.Client,
    resolved: resolver.ResolvedSlashCommand,
    channel,
    arguments: dict,
    *,
    application_id: int,
    command_name: Optional[str] = None,
    timeout: float = 10.0,
) -> Execution:
    name = command_name or " ".join(resolved.target)
    match = Match(
        channel_id=channel.id,
        author_id=application_id,
        invoker_id=client.user.id,
        command_name=name,
    )
    marker = discord.utils.time_snowflake(discord.utils.utcnow(), high=True)
    waiter = ResponseWaiter(client)

    await invoker.invoke(
        resolved.command,
        channel,
        convert_arguments(client, resolved.command, channel, arguments),
    )

    state = {"result": _UNSET}

    async def settle(timeout: float = timeout) -> Optional[HarvestedMessage]:
        if state["result"] is not _UNSET:
            return state["result"]
        result: Optional[HarvestedMessage] = None
        try:
            message = await waiter.wait_or_reconcile(
                match, channel=channel, marker_id=marker, timeout=timeout
            )
            message = await stabilize(message, client=client, channel=channel)
            result = await harvest_full(message, client)
            if _is_blank(result):
                try:
                    followup = await waiter.wait_or_reconcile(
                        match, channel=channel, marker_id=message.id, timeout=8.0
                    )
                    followup = await stabilize(followup, client=client, channel=channel)
                    candidate = await harvest_full(followup, client)
                    if not _is_blank(candidate):
                        result = candidate
                except InteractionTimeout:
                    pass
        except InteractionTimeout:
            result = None
        state["result"] = result
        return result

    return Execution(kind="slash", note=f"/{name} in #{channel.id}", settle=settle)


async def execute_prefix(
    client: discord.Client,
    channel,
    text: str,
    *,
    application_id: int,
    files: Optional[list[discord.File]] = None,
    reference=None,
    timeout: float = 10.0,
) -> Execution:
    match = Match(
        channel_id=channel.id, author_id=application_id, interaction_only=False
    )
    marker = discord.utils.time_snowflake(discord.utils.utcnow(), high=True)
    waiter = ResponseWaiter(client)
    if len(text) > 2000:
        text = text[:2000]
    await channel.send(text, files=files or None, reference=reference)
    state = {"result": _UNSET}

    async def settle(timeout: float = timeout) -> Optional[HarvestedMessage]:
        if state["result"] is not _UNSET:
            return state["result"]
        result: Optional[HarvestedMessage] = None
        try:
            message = await waiter.wait_or_reconcile(
                match, channel=channel, marker_id=marker, timeout=timeout
            )
            message = await stabilize(message, client=client, channel=channel)
            result = await harvest_full(message, client)
            if _is_blank(result):
                try:
                    followup = await waiter.wait_or_reconcile(
                        match, channel=channel, marker_id=message.id, timeout=8.0
                    )
                    followup = await stabilize(followup, client=client, channel=channel)
                    candidate = await harvest_full(followup, client)
                    if not _is_blank(candidate):
                        result = candidate
                except InteractionTimeout:
                    pass
        except InteractionTimeout:
            result = None
        state["result"] = result
        return result

    return Execution(
        kind="prefix", note=f"prefix '{text[:40]}' in #{channel.id}", settle=settle
    )
