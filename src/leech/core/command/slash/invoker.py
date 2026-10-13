import discord
from .error import SlashError


async def invoke(
    command: discord.SlashCommand | discord.SubCommand,
    channel: discord.abc.Messageable,
    arguments: dict[str, object],
) -> discord.Interaction:
    try:
        return await command(channel, **arguments)
    except Exception as e:
        raise SlashError(f"slash command invocation failed, details: '{e}'") from e
