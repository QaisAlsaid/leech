import pytest

from leech.core.command.binder import CommandBinder
from leech.core.command.registry import CommandRegistry
from leech.core.command.runtime import Command
from leech.core.command.model import CommandDescriptor, ArgumentDescriptor
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.error import BaseError
from leech.core.parser.types.definition import Types
from leech.core.command.self.binding import SelfBinding
from leech.core.command.binding import Binding


def test_bind_self_command():
    registry = CommandRegistry()

    target = Command(
        name="ping",
        description="Ping",
        arguments=[],
        subcommands={},
        binding=None,  # pyright: ignore
    )

    registry.register_command("ping", target)

    descriptor = CommandDescriptor(
        name="alias",
        description="Alias",
        provider=ProviderDescriptor(
            type=ProviderType.SELF,
            target=["ping"],
        ),
    )

    binder = CommandBinder(registry)

    result = binder.bind(descriptor)

    assert result.name == "alias"
    assert result.description == "Alias"
    assert isinstance(result.binding, SelfBinding)
    assert result.binding.target is target
    assert result.arguments is target.arguments
    assert result.subcommands is target.subcommands


def test_self_command_cannot_override_arguments():
    registry = CommandRegistry()

    target = Command(
        name="ping",
        description="Ping",
        arguments=[],
        subcommands={},
        binding=None,  # pyright: ignore
    )

    registry.register_command("ping", target)

    descriptor = CommandDescriptor(
        name="alias",
        description="Alias",
        arguments=[
            ArgumentDescriptor(
                name="foo",
                type=Types.STR,
            )
        ],
        provider=ProviderDescriptor(
            type=ProviderType.SELF,
            target=["ping"],
        ),
    )

    with pytest.raises(BaseError):
        CommandBinder(registry).bind(descriptor)


def test_self_command_cannot_override_subcommands():
    registry = CommandRegistry()

    target = Command(
        name="ping",
        description="Ping",
        arguments=[],
        subcommands={},
        binding=None,  # pyright: ignore
    )

    registry.register_command("ping", target)

    descriptor = CommandDescriptor(
        name="alias",
        description="Alias",
        subcommands={
            "foo": CommandDescriptor(
                name="foo",
                description="Foo",
                provider=ProviderDescriptor(
                    type=ProviderType.SELF,
                    target=["ping"],
                ),
            )
        },
        provider=ProviderDescriptor(
            type=ProviderType.SELF,
            target=["ping"],
        ),
    )

    with pytest.raises(BaseError):
        CommandBinder(registry).bind(descriptor)


def test_self_command_requires_existing_target():
    registry = CommandRegistry()

    descriptor = CommandDescriptor(
        name="alias",
        description="Alias",
        provider=ProviderDescriptor(
            type=ProviderType.SELF,
            target=["missing"],
        ),
    )

    with pytest.raises(BaseError):
        CommandBinder(registry).bind(descriptor)


class _TestBinding(Binding):
    async def invoke(self, invocation):
        return None


class _TestCommandBinder(CommandBinder):
    def bind_provider(self, descriptor):
        return _TestBinding()


def test_bind_recursively_binds_subcommands():
    registry = CommandRegistry()

    descriptor = CommandDescriptor(
        name="admin",
        description="Admin",
        subcommands={
            "echo": CommandDescriptor(
                name="echo",
                description="Echo",
                provider=ProviderDescriptor(
                    type=ProviderType.PREFIX,
                    target=["echo"],
                ),
            )
        },
        provider=ProviderDescriptor(
            type=ProviderType.PREFIX,
            target=["admin"],
        ),
    )

    result = _TestCommandBinder(registry).bind(descriptor)

    assert result.name == "admin"
    assert "echo" in result.subcommands
    assert result.subcommands["echo"].name == "echo"
