import discord
from enum import Enum, auto
from .error import LexError
from .token import Token, TokenType


class Triggers(Enum):
    NONE = auto()
    PREFIX = auto()
    MENTION = auto()


class LexedMessage:
    def __init__(
        self, trigger: Triggers, tokens: list[Token] = [], prefix: str | None = None
    ):
        self.trigger: Triggers = trigger
        self.tokens: list[Token] = tokens
        self.prefix: str | None = prefix


def lex_string(raw: str, index: int, char: str) -> tuple[str, TokenType, int]:
    out = ""
    index += 1

    while index < len(raw) and raw[index] != char:
        out += raw[index]
        index += 1

    if index >= len(raw):
        raise LexError("you have provided an unterminated string")

    return out, TokenType.STR, index + 1


def lex_assign(raw: str, index: int, char: str) -> tuple[str, TokenType, int]:
    return (char, TokenType.ASSIGN, index + 1)


def lex_mention(raw: str, index: int, char: str) -> tuple[str, TokenType, int]:
    # user: <@id> <@!id>
    # channels: <#id>
    # roles: <@&>

    if index + 1 >= len(raw):
        return char, TokenType.RAW, index + 1

    i = index + 1

    if raw[i] == "#":
        type = TokenType.CHANNEL
        i += 1

    elif raw[i] == "@":
        i += 1

        if i < len(raw) and raw[i] == "!":
            type = TokenType.USER
            i += 1
        elif i < len(raw) and raw[i] == "&":
            type = TokenType.ROLE
            i += 1
        else:
            type = TokenType.USER

    else:
        return char, TokenType.RAW, index + 1

    id = ""

    while i < len(raw) and raw[i] != ">":
        if not raw[i].isdigit():
            return char, TokenType.RAW, index + 1

        id += raw[i]
        i += 1

    # no closing >
    if i >= len(raw):
        return char, TokenType.RAW, index + 1

    # id not int
    if not id:
        return char, TokenType.RAW, index + 1

    return id, type, i + 1


def lex(
    content: str,
    prefixes: list[str],
    bot_id: int | None = None,
    continue_on_no_trigger: bool = True,
) -> LexedMessage:
    raw = content.strip()
    prefix = None
    trigger = Triggers.NONE

    for candidate in sorted(prefixes, key=len, reverse=True):
        if raw.startswith(candidate):
            raw = raw[len(candidate) :].strip()
            prefix = candidate
            trigger = Triggers.PREFIX
            break

    if trigger == Triggers.NONE:
        if bot_id is not None:
            import re

            cleaned = re.sub(rf"<@!?{str(bot_id)}>", "", raw, count=1)
            if len(cleaned) != len(raw):
                trigger = Triggers.MENTION
                raw = cleaned

    if trigger == Triggers.NONE and continue_on_no_trigger == False:
        return LexedMessage(trigger, [])

    tokens: list[Token] = []
    next_raw: str = ""
    i = 0

    def try_append(value: str, type: TokenType, index: int):
        nonlocal i, next_raw, tokens
        if type == TokenType.RAW:
            next_raw += value
        else:
            if next_raw:
                tokens.append(Token(type=TokenType.RAW, raw=next_raw))
                next_raw = ""

            if type != TokenType.SKIP:
                tokens.append(Token(type=type, raw=value))

        i = index

    while i < len(raw):
        char = raw[i]
        match char:
            case '"':
                try_append(*lex_string(raw, i, char))
            case "'":
                try_append(*lex_string(raw, i, char))
            case "=":
                try_append(*lex_assign(raw, i, char))
            case "<":
                try_append(*lex_mention(raw, i, char))
            case _:
                if char.isspace():
                    try_append("", TokenType.SKIP, i + 1)
                else:
                    try_append(char, TokenType.RAW, i + 1)
    if next_raw:
        tokens.append(Token(type=TokenType.RAW, raw=next_raw))
    return LexedMessage(trigger=trigger, prefix=prefix, tokens=tokens)
