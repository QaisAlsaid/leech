import pytest

from leech.core.command.dispatcher import CommandDispatcher
from leech.core.command.registry import CommandRegistry
from leech.core.command.invocation import (
    Invocation,
)  # v10: Invocation lives here, not in runtime
from leech.core.command.runtime import Command
from leech.core.command.native.binding import NativeBinding
from leech.core.parser.token import Token, TokenType


class FakeMessage:
    def __init__(self):
        self.replies = []
        self.channel = None  # v10: dispatcher typing-pulse reads message.channel

    async def reply(self, content, mention_author=False):
        self.replies.append((content, mention_author))


class FakeCogs:  # v10: dispatcher consults client.cogs.on_message
    async def on_message(self, message):  # when tokens don't resolve to a command
        return False, None


class FakeClient:
    cogs = FakeCogs()


def raw(value: str) -> Token:
    return Token(TokenType.RAW, value)


@pytest.mark.asyncio
async def test_dispatcher_executes_command():
    received = []

    async def handler(invocation):
        received.append(invocation)

    command = Command(
        name="ping",
        description="Ping",
        arguments=[],
        subcommands={},
        binding=NativeBinding(handler),
    )

    registry = CommandRegistry()
    assert registry.register_command("ping", command)

    dispatcher = CommandDispatcher(registry, FakeClient())
    message = FakeMessage()

    result = await dispatcher.dispatch(
        message,  # pyright: ignore
        [raw("ping")],
    )

    assert result == (True, None)
    assert len(received) == 1
    assert received[0].command is command
    assert received[0].arguments == {}
    assert message.replies == []


class RecordingBinding(NativeBinding):
    def __init__(self, received):
        self.received = received

    async def invoke(self, invocation):
        self.received.append(invocation)
        return None


@pytest.mark.asyncio
async def test_dispatcher_follows_invocation_chain():
    received = []

    target = Command(
        name="target",
        description="Target",
        arguments=[],
        subcommands={},
        binding=RecordingBinding(received),
    )

    class ChainBinding(NativeBinding):
        async def invoke(self, invocation):  # pyright: ignore
            return Invocation(
                message=invocation.message,
                command=target,
                arguments=invocation.arguments,
            )

    source = Command(
        name="source",
        description="Source",
        arguments=[],
        subcommands={},
        binding=ChainBinding(),
    )

    registry = CommandRegistry()
    assert registry.register_command("source", source)

    dispatcher = CommandDispatcher(registry, FakeClient())
    message = FakeMessage()

    await dispatcher.dispatch(message, [raw("source")])  # pyright: ignore

    assert len(received) == 1
    assert received[0].command is target
