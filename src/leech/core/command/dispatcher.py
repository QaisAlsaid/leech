import discord

from leech.core.parser.token import Token, TokenType

from .registry import CommandRegistry
from .invocation import Invocation
from . import resolver
from leech.core.parser import parser
from leech.core.error import BaseError

# from leech.leech import Leech

CHAIN_MAX = 64


class CommandDispatcher:
    def __init__(self, registry: CommandRegistry, client):
        self.registry = registry
        self.client = client

    async def error(
        self, message: discord.Message, error_message: str, is_user_error: bool = True
    ):
        title = "your fault 🚫🧠" if is_user_error else "my fault 🐛😁"
        await message.reply(
            f"**{title}**\n\n" "\n >".join(error_message.split("\n")),
            mention_author=True,
        )

    async def dispatch4(self, message: discord.Message, tokens: list[Token]):
        invocation = None
        consumed = False

        try:
            if tokens:
                if tokens[0].type != TokenType.RAW or not tokens[0].raw.isidentifier():
                    await self.error(
                        message,
                        f"you have provided an invalid command identifier: "
                        f"'{tokens[0].raw}'.\n"
                        "maybe try ``>help concepts identifiers``",
                    )
                    return False, None

                resolved = resolver.resolve_command(tokens, self.registry)

                if resolved is not None:
                    invocation = Invocation.parse(
                        message=message,
                        command=resolved.command,
                        tokens=resolved.args,
                    )
                    consumed = True
                else:
                    consumed, invocation = await self.client.cogs.on_message(message)

                    if not consumed:
                        await self.error(
                            message,
                            "you have provided an invalid command\n"
                            "maybe try ``>help``?",
                        )
                        return False, None
            else:
                consumed, invocation = await self.client.cogs.on_message(message)

                if not consumed:
                    return False, None

            chain = 0

            while invocation is not None:
                if chain >= CHAIN_MAX:
                    raise BaseError(
                        f"command chain is too long: {CHAIN_MAX}, "
                        f"limit is: {CHAIN_MAX}.\n"
                        "maybe try "
                        "`>help concepts command-chain`."
                    )

                async with message.channel.typing():
                    invocation = await invocation.invoke()

                chain += 1

        except BaseError as err:
            await self.error(message, err.message, err.is_user_error)
            return True, None

        return consumed, None
