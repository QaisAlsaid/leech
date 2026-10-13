import discord

from .error import SlashError


def validate(
    command: discord.SlashCommand | discord.SubCommand, arguments: dict[str, object]
):
    options = {option.name: option for option in command.options}

    for name in arguments:
        if name not in options:
            raise SlashError(
                f"unknown argument: '{name}' for slash command: '{command.name}'."
            )

    for option in options.values():
        if option.required and option.name not in arguments:
            raise SlashError(f"missing required argument: '{option.name}'.")
