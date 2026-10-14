import discord

from leech.core.command.runtime import Argument
from leech.core.parser.types.registry import Types, TypesRegistry
from .error import SlashError


def unsupported(t):
    raise SlashError(f"unsupported discord option type: '{t}'.")


def argument_type_from_option_type(
    optiontype: discord.ApplicationCommandOptionType, permissive: bool = False
) -> Types | None:
    match optiontype:
        case discord.ApplicationCommandOptionType.subcommand:
            return None
        case discord.ApplicationCommandOptionType.subcommand_group:
            return None
        case discord.ApplicationCommandOptionType.string:
            return Types.STR
        case discord.ApplicationCommandOptionType.integer:
            return Types.INT
        case discord.ApplicationCommandOptionType.boolean:
            return Types.BOOL
        case discord.ApplicationCommandOptionType.user:
            return Types.USER
        case discord.ApplicationCommandOptionType.channel:
            return Types.ATTACHMENT
        case discord.ApplicationCommandOptionType.role:
            if not permissive:
                unsupported(optiontype)
            return None
        case discord.ApplicationCommandOptionType.mentionable:
            if not permissive:
                unsupported(optiontype)
            return None
        case discord.ApplicationCommandOptionType.number:
            return Types.NUM
        case discord.ApplicationCommandOptionType.attachment:
            return Types.ATTACHMENT


def argument_from_option(
    option: discord.Option, permissive: bool = False
) -> Argument | None:
    type = argument_type_from_option_type(option.type, permissive=permissive)
    if type is None:
        return None

    typedef = TypesRegistry.require(type)
    forced_optional = option.type == discord.ApplicationCommandOptionType.attachment
    return Argument(
        name=option.name,
        type=typedef,
        description=option.description,
        required=option.required and not forced_optional,
    )
