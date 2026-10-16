from dataclasses import dataclass, field
from typing import Any

from leech.core.command.runtime import Argument, ResolvedCommand
from leech.core.parser.token import Token, TokenType
from .error import ParseError
from .types.definition import Cardinality
from .types.registry import TypesRegistry


@dataclass
class ParseContext:
    arguments: list[Argument]
    tokens: list[Token]
    defaults: dict[str, Any] = field(default_factory=dict)


class Parser:
    def __init__(self, context: ParseContext):
        self.context: ParseContext = context
        self.resolved: dict[str, Any] = {}
        self.current_positional_index: int = 0

    def current(self):
        while self.current_positional_index < len(self.context.arguments):
            arg = self.context.arguments[self.current_positional_index]

            if arg.name in self.resolved or arg.name in self.context.defaults:
                self.current_positional_index += 1
                continue

            return arg

        raise ParseError("you have provided too many arguments")

    # def current(self) -> Argument:
    #     for index in range(self.current_positional_index, len(self.context.arguments)):
    #         arg = self.context.arguments[index]
    #
    #         if arg.name in self.resolved:
    #             continue
    #
    #         has_default = arg.name in self.context.defaults
    #         effectively_optional = not arg.required or has_default
    #
    #         later_required = any(
    #             candidate.required
    #             and candidate.name not in self.resolved
    #             and candidate.name not in self.context.defaults
    #             for candidate in self.context.arguments[index + 1 :]
    #         )
    #
    #         if effectively_optional and later_required:
    #             continue
    #
    #         self.current_positional_index = index
    #         return arg
    #
    #     raise ParseError("you have provided too many arguments")
    #
    # def current(self):
    #    if self.current_positional_index >= len(self.context.arguments):
    #        raise ParseError("you have provided too many arguments")
    #    return self.context.arguments[self.current_positional_index]

    def is_last_argument(self, remaining: list[Token], arg: Argument):
        others = [
            desc
            for desc in self.context.arguments
            if desc.name != arg.name
            and desc.name not in self.resolved
            and desc.name not in self.context.defaults
        ]

        if not others:
            return True

        for token in remaining:
            if token.type == TokenType.ASSIGN:
                return False

        for desc in others:
            if desc.required:
                return False

        return True

    # def is_last_argument(self, remaining: list[Token], arg: Argument):
    #     if len(self.resolved) + 1 >= len(self.context.arguments):
    #         return True
    #
    #     for token in remaining:
    #         if token.type == TokenType.ASSIGN:
    #             return False
    #
    #     for desc in self.context.arguments:
    #         if (
    #             desc.required
    #             and desc.name not in self.resolved
    #             and desc.name not in self.context.defaults
    #         ):
    #             return False
    #     return True
    #

    def parse_named(
        self,
        name: Token,
        remaining: list[Token],
        arg: Argument,
        index: int,
    ) -> tuple[int, Any]:
        if len(remaining) < 2:
            raise ParseError(
                "you have provided an illegal '=', assign should have a value."
            )

        if remaining[1].type == TokenType.ASSIGN:
            raise ParseError(
                "you have provided an illegal '=', " "assign should have a valid value."
            )

        type = TypesRegistry.require(arg.type.type)

        if type is None:
            raise ParseError("invalid typename.", is_user_error=False)

        if type.cardinality == Cardinality.MANY:
            end = 2

            while end < len(remaining):
                if (
                    end + 1 < len(remaining)
                    and remaining[end].type == TokenType.RAW
                    and remaining[end + 1].type == TokenType.ASSIGN
                    and any(
                        candidate.name == remaining[end].raw
                        for candidate in self.context.arguments
                    )
                ):
                    break

                end += 1

            raw = " ".join(token.raw for token in remaining[1:end])

            if not raw:
                raise ParseError(
                    "you have provided an illegal '=', assign should have a value."
                )

            index += end
        else:
            raw = remaining[1].raw
            index += 2

        return index, type.parse(raw, arg)

    def check_named(self, token: Token) -> Argument:
        found = None

        for arg in self.context.arguments:
            if arg.name == token.raw:
                found = arg

        if found is None:
            raise ParseError(
                f"you have provided an invalid argument name: '{token.raw}'."
            )

        if found.name in self.resolved:
            raise ParseError(f"you have already set this argument: '{found.name}'.")

        return found

    def populate_defaults(self):
        for arg in self.context.arguments:
            if arg.name in self.resolved:
                continue

            if arg.name in self.context.defaults:
                self.resolved[arg.name] = self.context.defaults[arg.name]

    def check_missing(self):
        for arg in self.context.arguments:
            if (
                arg.required
                and arg.name not in self.resolved
                and arg.name not in self.context.defaults
            ):
                raise ParseError(f"you are missing a required argument: {arg.name}.")

    def parse(self):
        i = 0

        while i < len(self.context.tokens):
            token = self.context.tokens[i]

            if token.type == TokenType.ASSIGN:
                raise ParseError(
                    "you have provided an illegal '=', "
                    "and identifier must come before '='."
                )

            if (
                token.type == TokenType.RAW
                and i + 1 < len(self.context.tokens)
                and self.context.tokens[i + 1].type == TokenType.ASSIGN
            ):
                i, value = self.parse_named(
                    token,
                    self.context.tokens[i + 1 :],
                    self.check_named(token),
                    i + 1,
                )

                self.resolved[token.raw] = value
                continue

            current = self.current()
            type = TypesRegistry.require(current.type.type)

            if type is None:
                raise ParseError("invalid typename.", is_user_error=False)

            if type.cardinality == Cardinality.MANY:
                end = i + 1

                while end < len(self.context.tokens):
                    if (
                        end + 1 < len(self.context.tokens)
                        and self.context.tokens[end].type == TokenType.RAW
                        and self.context.tokens[end + 1].type == TokenType.ASSIGN
                        and any(
                            candidate.name == self.context.tokens[end].raw
                            for candidate in self.context.arguments
                        )
                    ):
                        break

                    end += 1

                if end < len(self.context.tokens):
                    raw = " ".join(token.raw for token in self.context.tokens[i:end])

                    self.resolved[current.name] = type.parse(raw, current)

                    self.current_positional_index += 1
                    i = end
                    continue

                if self.is_last_argument(
                    self.context.tokens[i:],
                    current,
                ):
                    raw = " ".join(token.raw for token in self.context.tokens[i:])

                    self.resolved[current.name] = type.parse(raw, current)

                    self.current_positional_index += 1
                    i = len(self.context.tokens)
                    continue

            self.resolved[current.name] = type.parse(token.raw, current)

            self.current_positional_index += 1
            i += 1

        self.populate_defaults()
        return self.resolved


def parse_args(context: ParseContext, check_missing: bool = True) -> dict[str, Any]:
    parser = Parser(context=context)
    resolved = parser.parse()
    if check_missing:
        parser.check_missing()

    return resolved
