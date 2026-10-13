import pytest

from leech.core.command.runtime import Command, Invocation
from leech.core.command.self.binding import SelfBinding
from leech.core.command.native.binding import NativeBinding
from leech.core.command.slash.binding import SlashBinding


class DummyMessage:
    pass


@pytest.mark.asyncio
async def test_native_binding_invokes_handler():
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

    invocation = Invocation(
        message=DummyMessage(),  # pyright: ignore
        command=command,
        arguments={"foo": "bar"},
    )

    result = await invocation.invoke()

    assert result is None
    assert received == [invocation]


@pytest.mark.asyncio
async def test_self_binding_returns_next_invocation():
    target = Command(
        name="ping",
        description="Ping",
        arguments=[],
        subcommands={},
        binding=NativeBinding(),
    )

    alias = Command(
        name="pong",
        description="Pong",
        arguments=[],
        subcommands={},
        binding=SelfBinding(target),
    )

    message = DummyMessage()

    invocation = Invocation(
        message=message,  # pyright: ignore
        command=alias,
        arguments={"foo": "bar"},
    )

    result = await invocation.invoke()

    assert result is not None
    assert result.message is message
    assert result.command is target
    assert result.arguments == {"foo": "bar"}
