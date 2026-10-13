import discord

from typing import Any, Callable
from .binding import Binding

from leech.core.parser.types.definition import TypeDefinition
from leech.core.parser.token import Token
from leech.core.provider.runtime import Provider


class Argument:
    def __init__(
        self,
        name: str,
        type: TypeDefinition,
        description: str = "",
        required: bool = True,
        usage_example: str = "",
        help_content: str = "",
    ):
        self.name: str = name
        self.type: TypeDefinition = type
        self.description = description
        self.required = required
        self.usage_example: str = usage_example
        self.help_content: str = help_content

    def __str__(self) -> str:
        return (
            f"{self.name}:{self.type.type.name} {self.description}" + " required"
            if self.required
            else ""
        )


class Command:
    def __init__(
        self,
        name: str,
        description: str,
        arguments: list[Argument],
        subcommands: dict[str, "Command"],
        binding: "Binding",
        is_self: bool = False,
        defaults: dict[str, Any] | None = None,
        usage_example: str = "",
        help_content: str = "",
        is_public: bool = True,
        is_root: bool = False,
        ai_visible: bool | None = None,
        elevated: bool = False,
    ):
        self.name = name
        self.description = description
        self.arguments = arguments
        self.subcommands = subcommands
        self.binding = binding
        self.defaults = defaults or {}
        self.usage_example: str = usage_example
        self.help_content: str = help_content
        self.is_public: bool = is_public
        self.is_root = is_root
        self.ai_visible = ai_visible
        self.elevated = elevated


class ResolvedCommand:
    def __init__(self, command: Command, args: list[Token], path: str = ""):
        self.command: Command = command
        self.args: list[Token] = args
        self.path: str = path
