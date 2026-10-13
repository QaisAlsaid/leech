from .runtime import Command

import logging

_logger = logging.getLogger(__name__)


class CommandRegistry:
    registered_commands: dict[str, Command]

    def __init__(self):
        self.registered_commands = {}

    def register_command(self, path: str, cmd: Command) -> bool:
        paths = []

        def register(path: str, cmd: Command):
            if path in self.registered_commands:
                _logger.warning(f"command: '{path}' is already registered.")
                return False

            paths.append((path, cmd))

            for name, child in cmd.subcommands.items():
                if not register(f"{path}.{name}", child):
                    return False

            return True

        if not register(path, cmd):
            _logger.error(f"failed to register command: '{path}'.")
            return False

        for path, cmd in paths:
            self.registered_commands[path] = cmd
            _logger.debug(f"registered command: '{path}'.")

        return True

    def unregister_command(self, path: str) -> Command | None:
        cmd = self.registered_commands.pop(path, None)
        if cmd is None:
            _logger.error(f"failed to unregister command: '{path}'.")
            return None
        for name, child in cmd.subcommands.items():
            if self.unregister_command(f"{path}.{name}") is None:
                _logger.error(f"failed to unregister command: {path}.{name}.")
                status = False
            else:
                _logger.debug(f"unregistered command: '{path}.{name}'.")
        return cmd

    def get_command(self, path: str) -> Command | None:
        return self.registered_commands.get(path)
