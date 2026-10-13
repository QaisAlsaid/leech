from leech.core.command.registry import CommandRegistry
from leech.core.command.resolver import resolve_command
from leech.core.command.runtime import Command
from leech.core.parser.token import Token, TokenType


def command(name: str, subcommands=None) -> Command:
    return Command(
        name=name,
        description="test",
        arguments=[],
        subcommands=subcommands or {},
        binding=None,  # pyright: ignore
    )


def raw(value: str) -> Token:
    return Token(TokenType.RAW, value)


def assign() -> Token:
    return Token(TokenType.ASSIGN, "=")


def test_resolve_root_command():
    registry = CommandRegistry()
    cmd = command("sudo")

    registry.register_command("sudo", cmd)

    result = resolve_command([raw("sudo")], registry)

    assert result is not None
    assert result.command is cmd
    assert result.args == []


def test_resolve_subcommand():
    registry = CommandRegistry()

    echo = command("echo")
    sudo = command("sudo", {"echo": echo})

    registry.register_command("sudo", sudo)

    result = resolve_command(
        [raw("sudo"), raw("echo"), raw("hello")],
        registry,
    )

    assert result is not None
    assert result.command is echo
    assert [token.raw for token in result.args] == ["hello"]


def test_named_argument_stops_subcommand_resolution():
    registry = CommandRegistry()

    echo = command("echo")
    sudo = command("sudo", {"echo": echo})

    registry.register_command("sudo", sudo)

    result = resolve_command(
        [raw("sudo"), raw("echo"), assign(), raw("foo")],
        registry,
    )

    assert result is not None
    assert result.command is sudo
    assert [token.raw for token in result.args] == [
        "echo",
        "=",
        "foo",
    ]


def test_unknown_subcommand_becomes_argument():
    registry = CommandRegistry()

    sudo = command("sudo")
    registry.register_command("sudo", sudo)

    result = resolve_command(
        [raw("sudo"), raw("hello")],
        registry,
    )

    assert result is not None
    assert result.command is sudo
    assert [token.raw for token in result.args] == ["hello"]


def test_unknown_root_command_returns_none():
    registry = CommandRegistry()

    result = resolve_command(
        [raw("missing")],
        registry,
    )

    assert result is None


def test_empty_tokens_returns_none():
    registry = CommandRegistry()

    assert resolve_command([], registry) is None
