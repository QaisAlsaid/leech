from typing import override

from leech.core.command.binding import Binding
from leech.core.command.invocation import Invocation
from leech.core.command.registry import CommandRegistry, is_valid_identifier
from leech.core.cog.error import CogError
from leech.core.parser import parser
import leech.core.parser.lexer as lexer
from leech.core.parser.token import TokenType
from leech.core.command import resolver


class SudoBinding(Binding):
    """the elevation modifier: takes a raw command line, re-resolves it, and
    derives the invocation with elevation attached.
    invariants: refuses via='ai' (elevation is a human gesture); owner-gated
    upstream by the default-deny policy on command:sudo; derive never forwards
    elevated — only this binding grants it."""

    def __init__(self, registry: CommandRegistry):
        self._registry = registry

    @override
    async def invoke(self, invocation: Invocation):
        if invocation.via == "ai":
            raise CogError("sudo is a human gesture 😁", is_user_error=True)

        line = invocation.arguments["command"]
        lexed = lexer.lex(line, prefixes=[])
        if not lexed.tokens:
            raise CogError("nothing to elevate.", is_user_error=True)
        if lexed.tokens[0].type != TokenType.RAW or not is_valid_identifier(
            lexed.tokens[0].raw
        ):
            raise CogError(
                f"'{lexed.tokens[0].raw}' is not a valid command identifier.",
                is_user_error=True,
            )

        resolved = resolver.resolve_command(lexed.tokens, self._registry)
        if resolved is None:
            raise CogError(f"unknown command: '{line}'.", is_user_error=True)

        arguments = parser.parse_args(
            parser.ParseContext(
                arguments=resolved.command.arguments, tokens=resolved.args
            )
        )

        inner = invocation.derive(resolved.command, arguments)
        inner.elevated = True
        return inner
