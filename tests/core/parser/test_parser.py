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


def test_unknown_named_argument_becomes_positional_text():
    # v10.8: `fork=hell` where `fork` is not an argument name used to
    # raise "invalid argument name". it is punctuation the user typed -
    # fuse it back into the positional text verbatim.
    cmd = command(
        Argument("name", StrType()),
    )

    result = parse(
        cmd,
        raw("fork"),
        assign(),
        raw("hell"),
    )

    assert values(result) == {
        "name": "fork=hell",
    }


def test_unknown_named_argument_does_not_eat_following_words():
    # `?x=y z` must come back out as two words, never `?x=yz`.
    cmd = command(
        Argument("name", StrType()),
    )

    result = parse(
        cmd,
        raw("a?x"),
        assign(),
        raw("y"),
        raw("z"),
    )

    assert values(result) == {
        "name": "a?x=y z",
    }


def test_unknown_named_argument_fuses_each_equals_run():
    # `?a=1&b=2` lexes as [?a, =, 1&b, =, 2] - all of it is one word.
    cmd = command(
        Argument("name", StrType()),
    )

    result = parse(
        cmd,
        raw("p?a"),
        assign(),
        raw("1&b"),
        assign(),
        raw("2"),
    )

    assert values(result) == {
        "name": "p?a=1&b=2",
    }


def test_unknown_named_argument_mixed_with_real_named():
    # an unquoted url with a query param inside a rest arg, followed by
    # a REAL named argument: both survive, each intact.
    cmd = command(
        Argument("task", StrType()),
        Argument("due", StrType()),
    )

    result = parse(
        cmd,
        raw("read"),
        raw("https://site.com/watch?v"),
        assign(),
        raw("abc"),
        raw("due"),
        assign(),
        raw("9am"),
    )

    assert values(result) == {
        "task": "read https://site.com/watch?v=abc",
        "due": "9am",
    }


def test_trailing_unknown_assign_without_value_is_text():
    cmd = command(
        Argument("name", StrType()),
    )

    result = parse(
        cmd,
        raw("foo"),
        raw("bar"),
        assign(),
    )

    assert values(result) == {
        "name": "foo bar=",
    }


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
