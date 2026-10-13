from leech.core.command.registry import CommandRegistry
from leech.core.command.runtime import Command


def command(name: str, subcommands=None) -> Command:
    return Command(
        name=name,
        description="test",
        arguments=[],
        subcommands=subcommands or {},
        binding=None,  # pyright: ignore
    )


def test_register_command():
    registry = CommandRegistry()
    cmd = command("ping")

    assert registry.register_command("ping", cmd)
    assert registry.get_command("ping") is cmd


def test_register_nested_commands():
    registry = CommandRegistry()

    echo = command("echo")
    admin = command("admin", {"echo": echo})

    assert registry.register_command("admin", admin)

    assert registry.get_command("admin") is admin
    assert registry.get_command("admin.echo") is echo


def test_duplicate_registration_fails():
    registry = CommandRegistry()

    first = command("ping")
    second = command("ping")

    assert registry.register_command("ping", first)
    assert not registry.register_command("ping", second)

    assert registry.get_command("ping") is first


def test_duplicate_nested_registration_is_transactional():
    registry = CommandRegistry()

    existing = command("admin.existing")
    assert registry.register_command("admin.existing", existing)

    new_child = command("new")
    conflicting_child = command("existing")

    admin = command(
        "admin",
        {
            "new": new_child,
            "existing": conflicting_child,
        },
    )

    assert not registry.register_command("admin", admin)

    # Neither the root nor the non-conflicting child should have been committed.
    assert registry.get_command("admin") is None
    assert registry.get_command("admin.new") is None
    assert registry.get_command("admin.existing") is existing


def test_unregister_command_removes_subtree():
    registry = CommandRegistry()

    echo = command("echo")
    admin = command("admin", {"echo": echo})

    assert registry.register_command("admin", admin)

    removed = registry.unregister_command("admin")

    assert removed is admin
    assert registry.get_command("admin") is None
    assert registry.get_command("admin.echo") is None


def test_unregister_missing_command_returns_none():
    registry = CommandRegistry()

    assert registry.unregister_command("missing") is None
