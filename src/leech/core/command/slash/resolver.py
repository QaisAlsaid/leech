from dataclasses import dataclass

import discord

from leech.core.parser.token import Token, TokenType
from .error import SlashError


@dataclass
class ResolvedSlashCommand:
    command: discord.SlashCommand | discord.SubCommand
    target: list[str]
    args: list[Token]


async def resolve(
    channel: discord.abc.Messageable, tokens: list[Token], bot: int
) -> ResolvedSlashCommand:
    try:
        commands = await channel.application_commands()
    except TypeError as e:
        raise SlashError(f"failed to fetch application commands, details: {e}") from e

    if not tokens:
        raise SlashError("missing slash command.")

    command = next(
        (
            command
            for command in commands
            if isinstance(command, discord.SlashCommand)
            and command.name == tokens[0].raw
            and command.application_id == bot
        ),
        None,
    )

    if command is None:
        raise SlashError(
            f"slash command: '{tokens[0].raw}' not found in bot id: '{bot}' in this channel: '{channel}'."
        )

    target = [command.name]
    index = 1
    while index < len(tokens):
        child = next(
            (child for child in command.children if child.name == tokens[index].raw),
            None,
        )
        if child is None:
            break

        command = child
        target.append(child.name)
        index += 1

    if command.is_group():
        raise SlashError(f"slash command: '{command.name}' can't be invoked.")

    return ResolvedSlashCommand(command, target, tokens[index:])


async def resolve_target(
    channel: discord.abc.Messageable, target: list[str], bot: int
) -> ResolvedSlashCommand:
    tokens = [Token(TokenType.RAW, name) for name in target]
    resolved = await resolve(channel, tokens, bot)
    if resolved.args:
        raise SlashError("extra tokens after target resolution.")
    return resolved
