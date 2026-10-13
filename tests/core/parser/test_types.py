import pytest

from leech.core.command.runtime import Argument
from leech.core.parser.error import ParseError
from leech.core.parser.types.definition import Cardinality, Types
from leech.core.parser.types.types import (
    BoolType,
    IntType,
    NumType,
    StrType,
)


class FakeArgument:
    def __init__(self, name: str):
        self.name = name


def test_int_type():
    arg = FakeArgument("count")

    assert IntType().parse("44", arg) == 44
    assert IntType().parse("-67", arg) == -67


def test_int_type_invalid():
    arg = FakeArgument("count")

    with pytest.raises(ParseError):
        IntType().parse("hello", arg)


def test_num_type():
    arg = FakeArgument("value")

    assert NumType().parse("44", arg) == 44.0
    assert NumType().parse("3.14", arg) == 3.14
    assert NumType().parse("-6.7", arg) == -6.7


def test_num_type_invalid():
    arg = FakeArgument("value")

    with pytest.raises(ParseError):
        NumType().parse("hello", arg)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("on", True),
        ("yes", True),
        ("false", False),
        ("FALSE", False),
        ("off", False),
        ("no", False),
    ],
)
def test_bool_type(raw, expected):
    arg = FakeArgument("enabled")

    assert BoolType().parse(raw, arg) is expected


def test_bool_type_invalid():
    arg = FakeArgument("enabled")

    with pytest.raises(ParseError):
        BoolType().parse("maybe", arg)


def test_str_type():
    arg = FakeArgument("message")

    type_ = StrType()

    assert type_.parse("hello", arg) == "hello"
    assert type_.parse("hello world", arg) == "hello world"
    assert type_.cardinality == Cardinality.MANY


def test_type_definitions_have_expected_names():
    assert IntType().type == Types.INT
    assert NumType().type == Types.NUM
    assert BoolType().type == Types.BOOL
    assert StrType().type == Types.STR
