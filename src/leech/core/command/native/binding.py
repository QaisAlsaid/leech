from typing import Awaitable, Callable, override
from leech.core.command.invocation import Invocation
from leech.core.command.binding import Binding

NativeHandler = Callable[[Invocation], Awaitable[Invocation | None]]


class NativeBinding(Binding):
    def __init__(self, handler: NativeHandler | None = None):
        self.handler: NativeHandler | None = handler

    @override
    async def invoke(self, invocation: Invocation):
        if self.handler is not None:
            return await self.handler(invocation)
