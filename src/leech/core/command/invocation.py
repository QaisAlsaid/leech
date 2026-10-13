import discord
from typing import Any, TYPE_CHECKING
from .runtime import Command
import leech.core.parser.parser as parser
from leech.core.parser.lexer import Token


class Chain:
    pass


class Invocation:
    def __init__(
        self,
        message: discord.Message,
        command: Command,
        arguments: dict[str, Any],
        defaults: dict[str, Any] | None = None,
        *,
        chain=None,
        site=None,
        via: str | None = None,
    ):
        self.message: discord.Message = message
        self.command: Command = command
        self.arguments: dict[str, Any] = arguments
        self.defaults = defaults or {}
        self.chain = chain or Chain()
        self.site = site
        self.via = via

    async def invoke(self) -> "Invocation | None":
        return await self.command.binding.invoke(self)

    @classmethod
    def create(
        cls,
        message: discord.Message,
        command: Command,
        arguments: dict[str, Any],
    ) -> "Invocation":
        return cls(
            message=message,
            command=command,
            arguments=arguments,
            defaults=dict(command.defaults),
        )

    def with_command(self, command: Command) -> "Invocation":
        defaults = dict(self.defaults)
        defaults.update(command.defaults)

        return Invocation(
            message=self.message,
            command=command,
            arguments=self.arguments,
            defaults=defaults,
        )

    @classmethod
    def parse(
        cls,
        message: discord.Message,
        command: Command,
        tokens: list[Token],
        chain=None,
        via=None,
    ) -> "Invocation":
        defaults = dict(command.defaults)

        arguments = parser.parse_args(
            parser.ParseContext(
                arguments=command.arguments,
                tokens=tokens,
                defaults=defaults,
            )
        )

        return cls(
            message=message,
            command=command,
            arguments=arguments,
            defaults=defaults,
        )
