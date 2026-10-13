from typing import override

import discord

from leech.core.command.binding import Binding
from leech.core.command.invocation import Invocation
from .error import SlashError
from . import resolver, invoker


class SlashBinding(Binding):
    def __init__(self, target: list[str], bot: int):
        self.target: list[str] = target
        self.bot: int = bot

    @override
    async def invoke(self, invocation: Invocation):
        resolved = await resolver.resolve_target(
            invocation.message.channel, self.target, self.bot
        )
        await invoker.invoke(
            resolved.command, invocation.message.channel, invocation.arguments
        )
