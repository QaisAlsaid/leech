import pytest

from leech.core.command.runtime import Argument, Command, ResolvedCommand
from leech.core.parser.error import ParseError
from leech.core.parser.parser import ParseContext, parse_args
from leech.core.parser.token import Token, TokenType
from leech.core.parser.types.types import (
    BoolType,
    IntType,
    StrType,
)
from leech.core.command.native.binding import NativeBinding
from leech.core.parser.types.registry import TypesRegistry


def command(*arguments):
    return Command(
        name="foo",
        description="test command",
        arguments=list(arguments),
        subcommands={},
        binding=NativeBinding(),
    )


def raw(value):
    return Token(TokenType.RAW, value)


def assign():
    return Token(TokenType.ASSIGN, "=")


def parse(command_, *tokens):
    return parse_args(
        ParseContext(
            command_.arguments,
            list(tokens),
        )
    )


def values(result):
    return result


def test_positional_arguments():
    cmd = command(
        Argument("name", StrType()),
        Argument("count", IntType()),
    )

    result = parse(
        cmd,
        raw("hello"),
        raw("44"),
    )

    assert values(result) == {
        "name": "hello",
        "count": 44,
    }


def test_named_arguments():
    cmd = command(
        Argument("name", StrType()),
        Argument("count", IntType()),
    )

    result = parse(
        cmd,
        raw("name"),
        assign(),
        raw("hello"),
        raw("count"),
        assign(),
        raw("44"),
    )

    assert values(result) == {
        "name": "hello",
        "count": 44,
    }


def test_mixed_positional_and_named_arguments():
    cmd = command(
        Argument("name", StrType()),
        Argument("count", IntType()),
    )

    result = parse(
        cmd,
        raw("hello"),
        raw("count"),
        assign(),
        raw("44"),
    )

    assert values(result) == {
        "name": "hello",
        "count": 44,
    }


def test_named_then_positional():
    cmd = command(
        Argument("name", StrType()),
        Argument("count", IntType()),
    )

    result = parse(
        cmd,
        raw("count"),
        assign(),
        raw("44"),
        raw("hello"),
    )

    assert values(result) == {
        "count": 44,
        "name": "hello",
    }


def test_duplicate_named_argument():
    cmd = command(
        Argument("name", StrType()),
    )

    with pytest.raises(ParseError, match="already set"):
        parse(
            cmd,
            raw("name"),
            assign(),
            raw("hello"),
            raw("name"),
            assign(),
            raw("world"),
        )


def test_unknown_named_argument():
    cmd = command(
        Argument("name", StrType()),
    )

    with pytest.raises(ParseError, match="invalid argument name"):
        parse(
            cmd,
            raw("fork"),
            assign(),
            raw("hell"),
        )


def test_missing_required_argument():
    cmd = command(
        Argument("name", StrType()),
        Argument("count", IntType()),
    )

    with pytest.raises(ParseError, match="missing a required argument"):
        parse(
            cmd,
            raw("hello"),
        )


def test_too_many_positional_arguments_for_one_cardinality():
    cmd = command(
        Argument("count", IntType()),
    )

    with pytest.raises(ParseError, match="too many arguments"):
        parse(
            cmd,
            raw("6"),
            raw("7"),
        )


def test_positional_many_consumes_remaining_tokens():
    cmd = command(
        Argument("reason", StrType()),
    )

    result = parse(
        cmd,
        raw("this"),
        raw("is"),
        raw("a"),
        raw("reason"),
    )

    assert values(result) == {
        "reason": "this is a reason",
    }


def test_positional_many_does_not_steal_following_argument():
    cmd = command(
        Argument("reason", StrType()),
        Argument("count", IntType()),
    )

    result = parse(
        cmd,
        raw("hello"),
        raw("44"),
    )

    assert values(result) == {
        "reason": "hello",
        "count": 44,
    }


def test_positional_many_stops_at_named_argument():
    cmd = command(
        Argument("reason", StrType()),
        Argument("enabled", BoolType()),
    )

    result = parse(
        cmd,
        raw("this"),
        raw("is"),
        raw("a"),
        raw("reason"),
        raw("enabled"),
        assign(),
        raw("true"),
    )

    assert values(result) == {
        "reason": "this is a reason",
        "enabled": True,
    }


def test_explicit_named_string_can_consume_remainder():
    cmd = command(
        Argument("reason", StrType()),
        Argument("enabled", BoolType()),
    )

    result = parse(
        cmd,
        raw("reason"),
        assign(),
        raw("this"),
        raw("is"),
        raw("why"),
        raw("enabled"),
        assign(),
        raw("true"),
    )

    assert values(result) == {
        "reason": "this is why",
        "enabled": True,
    }


def test_named_many_stops_at_next_named_argument():
    cmd = command(
        Argument("reason", StrType()),
        Argument("enabled", BoolType()),
    )

    result = parse(
        cmd,
        raw("reason"),
        assign(),
        raw("this"),
        raw("is"),
        raw("why"),
        raw("enabled"),
        assign(),
        raw("false"),
    )

    assert values(result) == {
        "reason": "this is why",
        "enabled": False,
    }


def test_assignment_without_identifier():
    cmd = command(
        Argument("name", StrType()),
    )

    with pytest.raises(ParseError, match="identifier must come before"):
        parse(
            cmd,
            assign(),
            raw("hello"),
        )


def test_assignment_without_value():
    cmd = command(
        Argument("name", StrType()),
    )

    with pytest.raises(ParseError, match="assign should have a value"):
        parse(
            cmd,
            raw("name"),
            assign(),
        )


def test_assignment_followed_by_assignment():
    cmd = command(
        Argument("name", StrType()),
    )

    with pytest.raises(ParseError, match="assign should have a valid value"):
        parse(
            cmd,
            raw("name"),
            assign(),
            assign(),
        )


def test_invalid_int_is_parser_error():
    cmd = command(
        Argument("count", IntType()),
    )

    with pytest.raises(ParseError):
        parse(
            cmd,
            raw("fallujah"),
        )


def test_boolean_argument():
    cmd = command(
        Argument("enabled", BoolType()),
    )

    result = parse(
        cmd,
        raw("true"),
    )

    assert values(result) == {
        "enabled": True,
    }
