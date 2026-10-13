from .registry import CommandRegistry
from .runtime import Argument, Command, ResolvedCommand

from leech.core.parser.lexer import LexedMessage
from leech.core.parser.token import Token, TokenType
from leech.core.error import BaseError

import logging

_logger = logging.getLogger(__name__)


def resolve_command(
    tokens: list[Token], registry: CommandRegistry
) -> ResolvedCommand | None:
    if not tokens:
        return None

    path = tokens[0]
    path_raw = path.raw
    _logger.debug(f"resolving command: {path_raw}")
    command = registry.get_command(path_raw)
    if command is None:
        _logger.debug(f"command not found: {path_raw}")
        return None

    index = 1
    while index < len(tokens):
        if len(tokens) > index + 1:
            if (
                tokens[index].type == TokenType.RAW
                and tokens[index + 1].type == TokenType.ASSIGN
            ):
                break

        candidate = f"{path_raw}.{tokens[index].raw}"
        subcommand = registry.get_command(candidate)
        if subcommand is None:
            break

        path_raw = candidate
        command = subcommand
        index += 1

    _logger.debug(f"resolved command: path: {path_raw}, args: {tokens[index:]}")
    return ResolvedCommand(command=command, args=tokens[index:], path=path_raw)
