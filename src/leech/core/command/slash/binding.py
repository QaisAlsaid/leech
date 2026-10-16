from typing import override

from leech.core.command.binding import Binding
from leech.core.command.invocation import Invocation
from . import execution as slash_execution
from . import resolver


class SlashBinding(Binding):
    def __init__(self, target: list[str], bot: int):
        self.target: list[str] = target
        self.bot: int = bot

    @override
    async def invoke(self, invocation: Invocation):
        channel = invocation.message.channel
        client = invocation.message._state.client
        resolved = await resolver.resolve_target(channel, self.target, self.bot)
        execution = await slash_execution.execute_slash(
            client, resolved, channel, invocation.arguments, application_id=self.bot
        )
        invocation.chain.executions.append(execution)
        return None
