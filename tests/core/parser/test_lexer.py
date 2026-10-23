import pytest

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
    assert result.prefix is None
    # v10: no-trigger messages still lex (continue_on_no_trigger=True is the
    # default) - the dispatcher's cogs fallback path consumes these tokens.
    assert tokens(result) == [
        (TokenType.RAW, "hello"),
        (TokenType.RAW, "world"),
    ]

    # with continue_on_no_trigger=False the lexer short-circuits to no tokens.
    skipped = lex("hello world", PREFIXES, continue_on_no_trigger=False)
    assert skipped.trigger == Triggers.NONE
    assert skipped.tokens == []


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


def test_unterminated_double_quote_is_raw_text():
    # v10.8: a quote with no closing partner is punctuation, not a
    # string opener - `>todo add say "hello` used to die at lex time.
    result = lex('>foo say "hello', PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "say"),
        (TokenType.RAW, '"hello'),
    ]


def test_unterminated_single_quote_is_raw_text():
    # the apostrophe in `i mustn't larp` belongs to the word it sits in.
    result = lex(">todo add i mustn't larp", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "todo"),
        (TokenType.RAW, "add"),
        (TokenType.RAW, "i"),
        (TokenType.RAW, "mustn't"),
        (TokenType.RAW, "larp"),
    ]


def test_unmatched_quote_at_end_is_raw_text():
    result = lex(">foo larp'", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "larp'"),
    ]


def test_unmatched_quote_leaves_equals_lexing_alone():
    # `mustn't x=y` - the apostrophe is raw AND the `=` still lexes as
    # ASSIGN (the parser decides what to do with unknown names).
    result = lex(">foo mustn't x=y", PREFIXES)

    assert tokens(result) == [
        (TokenType.RAW, "foo"),
        (TokenType.RAW, "mustn't"),
        (TokenType.RAW, "x"),
        (TokenType.ASSIGN, "="),
        (TokenType.RAW, "y"),
    ]


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
