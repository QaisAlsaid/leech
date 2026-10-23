from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, override

from .registry import TypesRegistry
from .definition import Cardinality, TypeDefinition, Types
from leech.core.parser.error import ParseError

if TYPE_CHECKING:
    from leech.core.command.runtime import Argument


class IntType(TypeDefinition):
    def __init__(self):
        super().__init__(
            type=Types.INT,
            description="an integer type.",
            usage_example="value=44",
            help_content="an integer number, that is a whole number without decimal part.",
        )

    @override
    def parse(self, raw: str, arg: Argument) -> int:
        try:
            return int(raw)
        except ValueError as e:
            raise ParseError(
                f"you have provided an invalid int: '{raw}', for argument: '{arg.name}', which i failed to parse."
            ) from e


class NumType(TypeDefinition):
    def __init__(self):
        super().__init__(
            type=Types.NUM,
            description="a floating point type.",
            usage_example="value=6.7",
            help_content="a floating point number, that is decimal number.",
        )

    def parse(self, raw: str, arg: Argument) -> float:
        try:
            return float(raw)
        except ValueError as e:
            raise ParseError(
                f"you have provided an invalid number: '{raw}', for argument: '{arg.name}', which i failed to parse."
            ) from e


class BoolType(TypeDefinition):
    true = {"true", "on", "yes"}
    false = {"false", "off", "no"}

    def __init__(self):
        super().__init__(
            type=Types.BOOL,
            description=f"a boolean type.",
            usage_example="value=true",
            help_content=f"a boolean value, it can be either truthy or falsy where:\ntruthy values: {self.true}, and falsy values: {self.false}",
        )

    def parse(self, raw: str, arg: Argument) -> bool:
        low = raw.lower()
        if low in self.true:
            return True
        elif low in self.false:
            return False

        raise ParseError(
            f"you have provided an invalid boolean: '{raw}', for argument: '{arg.name}', which i failed to parse."
        )


class StrType(TypeDefinition):
    def __init__(self):
        super().__init__(
            type=Types.STR,
            description="a string type.",
            cardinality=Cardinality.MANY,
            usage_example="value='this is a quoted string'",
            help_content="a string type, it can represent either multiple unqouted values (where applicable), a quoted value.",
        )

    def parse(self, raw: str, arg: Argument) -> str:
        return raw


class WordType(TypeDefinition):
    def __init__(self):
        super().__init__(
            type=Types.WORD,
            description="a single word (quote it for spaces).",
            cardinality=Cardinality.ONE,
            usage_example="priority=high",
            help_content="one word, no spaces. use a quoted string for anything longer.",
        )

    def parse(self, raw: str, arg: Argument) -> str:
        return raw


class UserType(TypeDefinition):
    def __init__(self):
        super().__init__(
            type=Types.USER,
            description="a discord user.",
            usage_example="value=<@369201621399240725>",
            help_content="a type that is capable of represinting a discord user (raw numeric id or direct mention).",
        )

    def parse(self, raw: str, arg: Argument) -> int:
        try:
            return int(raw)
        except ValueError as e:
            raise ParseError(f"invalid user: '{raw}") from e


class ChannelType(TypeDefinition):
    def __init__(self):
        super().__init__(
            type=Types.CHANNEL,
            description="a discord channel.",
            usage_example="value=<#749353351530938370>",
            help_content="a type that is capable of represinting a discord channels (raw numeric id or direct mention).",
        )

    def parse(self, raw: str, arg: Argument) -> int:
        try:
            return int(raw)
        except ValueError as e:
            raise ParseError(f"invalid channel: '{raw}") from e


class RoleType(TypeDefinition):
    def __init__(self):
        super().__init__(
            type=Types.CHANNEL,
            description="a discord role.",
            usage_example="value=<@&749353351530938370>",
            help_content="a type that is capable of represinting a discord role (raw numeric id or direct mention).",
        )

    def parse(self, raw: str, arg: Argument) -> int:
        try:
            return int(raw)
        except ValueError as e:
            raise ParseError(f"invalid role: '{raw}") from e
