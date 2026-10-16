import discord
from typing import Any, Optional

from .runtime import Command
from .chain import Chain
import leech.core.parser.parser as parser
from leech.core.parser.lexer import Token


class Invocation:
    def __init__(
        self,
        message: discord.Message,
        command: Command,
        arguments: dict[str, Any],
        defaults: dict[str, Any] | None = None,
        *,
        chain: Optional[Chain] = None,
        via: Optional[str] = None,
        elevated: bool = False,
    ):
        self.message: discord.Message = message
        self.command: Command = command
        self.arguments: dict[str, Any] = arguments
        self.defaults: dict[str, Any] = defaults or {}
        self.chain: Chain = chain or Chain()
        self.via: Optional[str] = via
        self.elevated: bool = elevated

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

    def with_command(self, command: "Command") -> "Invocation":
        return self.derive(command, self.arguments)

    def derive(self, command: "Command", arguments: dict[str, Any]) -> "Invocation":
        defaults = dict(self.defaults)
        defaults.update(command.defaults)
        return Invocation(
            message=self.message,
            command=command,
            arguments=arguments,
            defaults=defaults,
            chain=self.chain,
            via=self.via,
        )

    @classmethod
    def parse(
        cls,
        message: discord.Message,
        command: Command,
        tokens: list[Token],
        *,
        chain: Optional[Chain] = None,
        via: Optional[str] = None,
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
            chain=chain,
            via=via,
        )
