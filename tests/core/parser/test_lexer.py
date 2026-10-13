import pytest

from leech.core.parser.error import LexError
from leech.core.parser.lexer import Triggers, lex
from leech.core.parser.token import TokenType

PREFIXES = [">", "!"]


def tokens(result):
    return [(token.type, token.raw) for token in result.tokens]


def test_simple_prefix_command():
    result = lex(">foo hello world", PREFIXES)

    assert result.trigger == Triggers.PREFIX
    assert result.prefix == ">"
    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "hello"),
        (TokenType.RAW, "world"),
    ]


def test_longest_prefix_wins():
    result = lex(">>foo hello", [">", ">>"])

    assert result.trigger == Triggers.PREFIX
    assert result.prefix == ">>"
    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "hello"),
    ]


def test_no_trigger():
    result = lex("hello world", PREFIXES)

    assert result.trigger == Triggers.NONE
    assert result.tokens == []


def test_named_argument():
    result = lex(">foo name=hello", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "name"),
        (TokenType.ASSIGN, "="),
        (TokenType.RAW, "hello"),
    ]


def test_multiple_named_arguments():
    result = lex(">foo name=hello count=44", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "name"),
        (TokenType.ASSIGN, "="),
        (TokenType.RAW, "hello"),
        (TokenType.RAW, "count"),
        (TokenType.ASSIGN, "="),
        (TokenType.RAW, "44"),
    ]


def test_quoted_string():
    result = lex('>foo "hello world"', PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.STR, "hello world"),
    ]


def test_single_quoted_string():
    result = lex(">foo 'hello world'", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.STR, "hello world"),
    ]


def test_equals_inside_quotes_is_not_assignment():
    result = lex('>foo "hello=world"', PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.STR, "hello=world"),
    ]


def test_equals_inside_named_quoted_value():
    result = lex('>foo reason="hello=world"', PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "reason"),
        (TokenType.ASSIGN, "="),
        (TokenType.STR, "hello=world"),
    ]


def test_unterminated_double_quote():
    with pytest.raises(LexError):
        lex('>foo "hello world', PREFIXES)


def test_unterminated_single_quote():
    with pytest.raises(LexError):
        lex(">foo 'hello world", PREFIXES)


@pytest.mark.parametrize(
    ("raw", "token_type", "value"),
    [
        ("<@123>", TokenType.USER, "123"),
        ("<@!123>", TokenType.USER, "123"),
        ("<@&123>", TokenType.ROLE, "123"),
        ("<#123>", TokenType.CHANNEL, "123"),
    ],
)
def test_discord_mentions(raw, token_type, value):
    result = lex(f">foo {raw}", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (token_type, value),
    ]


def test_invalid_mention_is_raw():
    result = lex(">foo <@hello>", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "<@hello>"),
    ]


def test_assignment_without_value_is_lexically_valid():
    result = lex(">foo name=", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "name"),
        (TokenType.ASSIGN, "="),
    ]


def test_mention_trigger():
    result = lex("<@123456> hello", [], bot_id=123456)

    assert result.trigger == Triggers.MENTION
    assert tokens(result) == [(TokenType.RAW, "hello")]


def test_prefix_shadows_mention_trigger():
    result = lex(">foo <@123456> hello", [">"], bot_id=123456)

    assert result.trigger == Triggers.PREFIX
    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.USER, "123456"),
        (TokenType.RAW, "hello"),
    ]
