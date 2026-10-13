from typing import override
from leech.core.command.invocation import Invocation
from leech.core.command.binding import Binding


class MockBinding(Binding):
    def __init__(self):
        pass

    @override
    async def invoke(self, invocation: Invocation):
        pass
