from discord import Message

from leech.core.cog.cog import Cog, CogContext
from leech.core.cog.error import CogError
from leech.core.command.runtime import Command
from leech.core.command.model import CommandDescriptor
from leech.core.command.invocation import Invocation
from leech.core.provider.model import ProviderType
from leech.core.command.native.binding import NativeBinding


class PingCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)

    async def load(self):
        command = Command(
            name="ping",
            description="ping the bot and get a response back",
            usage_example=">ping",
            help_content="simple command for ping.",
            arguments=[],
            subcommands={},
            binding=NativeBinding(handler=self.handler),
            is_root=True,
        )
        if not self.context.add_command("ping", command):
            raise CogError("failed to load ping command")

    async def handler(self, invocation: Invocation):
        await invocation.message.reply("pong")


def create_cog(context: CogContext):
    return PingCog(context=context)
