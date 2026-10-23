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

        if remaining[1].type == TokenType.STR:
            raw = remaining[1].raw
            index += 2
            return index, type.parse(raw, arg)

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

    def _fuse_unknown_named(self, tokens: list[Token]) -> list[Token]:
        """v10.8: `?x=y` in an unquoted link used to kill the parse.

        the lexer cuts an ASSIGN token at every `=`; when the word in
        front of one is NOT a known argument name there is no named
        argument there at all - it is punctuation the user typed inside
        positional text (a url query, `a==b`, `foo bar=x`). glue the `=`
        (and the directly-attached value word) back onto that word so
        the positional text reconstructs exactly what was typed.

        known names are left alone: `>todo add due=9am ...` still parses
        as a named argument, and repeating a known name still raises
        the "already set" error instead of silently becoming text."""
        names = {arg.name for arg in self.context.arguments}
        if not tokens:
            return tokens

        out: list[Token] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if (
                token.type == TokenType.RAW
                and token.raw not in names
                and i + 1 < len(tokens)
                and tokens[i + 1].type == TokenType.ASSIGN
            ):
                fused = token.raw
                i += 1
                # greedily absorb `=`-runs so the typed text comes back
                # out verbatim: `?a=1&b=2` lexes as [?a, =, 1&b, =, 2]
                # and must fuse into ONE word, not leave a stray `=`.
                # exactly one value word glues per `=` (no space): `?x=y z`
                # stays `?x=y z`, never `?x=yz`. a quoted value glues
                # unquoted; anything stranger (a mention) is left in the
                # stream for normal positional parsing.
                while i < len(tokens) and tokens[i].type == TokenType.ASSIGN:
                    fused += "="
                    i += 1
                    if i < len(tokens) and tokens[i].type in (
                        TokenType.RAW,
                        TokenType.STR,
                    ):
                        fused += tokens[i].raw
                        i += 1
                out.append(Token(type=TokenType.RAW, raw=fused))
                continue
            out.append(token)
            i += 1
        return out

    def parse(self):
        tokens = self._fuse_unknown_named(self.context.tokens)
        i = 0

        while i < len(tokens):
            token = tokens[i]

            if token.type == TokenType.ASSIGN:
                raise ParseError(
                    "you have provided an illegal '=', "
                    "and identifier must come before '='."
                )

            if (
                token.type == TokenType.RAW
                and i + 1 < len(tokens)
                and tokens[i + 1].type == TokenType.ASSIGN
            ):
                i, value = self.parse_named(
                    token,
                    tokens[i + 1 :],
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

                while end < len(tokens):
                    if (
                        end + 1 < len(tokens)
                        and tokens[end].type == TokenType.RAW
                        and tokens[end + 1].type == TokenType.ASSIGN
                        and any(
                            candidate.name == tokens[end].raw
                            for candidate in self.context.arguments
                        )
                    ):
                        break

                    end += 1

                if end < len(tokens):
                    raw = " ".join(token.raw for token in tokens[i:end])

                    self.resolved[current.name] = type.parse(raw, current)

                    self.current_positional_index += 1
                    i = end
                    continue

                if self.is_last_argument(
                    tokens[i:],
                    current,
                ):
                    raw = " ".join(token.raw for token in tokens[i:])

                    self.resolved[current.name] = type.parse(raw, current)

                    self.current_positional_index += 1
                    i = len(tokens)
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
