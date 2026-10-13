from typing import override

from leech.core.command.binding import Binding
from leech.core.command.runtime import Command
from leech.core.command.invocation import Invocation


class SelfBinding(Binding):
    def __init__(self, target: Command):
        self.target: Command = target

    @override
    async def invoke(self, invocation: Invocation):
        return invocation.with_command(self.target)
