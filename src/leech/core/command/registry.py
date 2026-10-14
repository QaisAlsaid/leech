from .runtime import Command

import logging

_logger = logging.getLogger(__name__)


class CommandRegistry:
    def __init__(self):
        self.registered_commands: dict[str, Command] = {}
        self._paths: dict[int, str] = (
            {}
        )  # id(Command) -> path (reverse index, P4 reads it)

    def register_command(self, path: str, cmd: Command) -> bool:
        if not path.isidentifier():
            _logger.error(f"rejected non-identifier command path: {path!r}")
            return False

        staged: list[tuple[str, Command]] = []

        def stage(path: str, cmd: Command) -> bool:
            if path in self.registered_commands:
                _logger.warning(f"command: '{path}' is already registered.")
                return False
            staged.append((path, cmd))
            for name, child in cmd.subcommands.items():
                if not name.isidentifier():
                    _logger.error(
                        f"rejected non-identifier subcommand: '{path}.{name}'."
                    )
                    return False
                if not stage(f"{path}.{name}", child):
                    return False
            return True

        if not stage(path, cmd):
            _logger.error(f"failed to register command: '{path}'.")
            return False

        for staged_path, staged_cmd in staged:
            self.registered_commands[staged_path] = staged_cmd
            self._paths[id(staged_cmd)] = staged_path
            _logger.debug(f"registered command: '{staged_path}'.")
        return True

    def unregister_command(self, path: str) -> Command | None:
        cmd = self.registered_commands.pop(path, None)
        if cmd is None:
            _logger.error(f"failed to unregister command: '{path}'.")
            return None
        self._paths.pop(id(cmd), None)
        for name, child in cmd.subcommands.items():
            self.unregister_command(f"{path}.{name}")
        return cmd

    def get_command(self, path: str) -> Command | None:
        return self.registered_commands.get(path)

    def path_of(self, command: Command) -> str | None:
        return self._paths.get(id(command))
