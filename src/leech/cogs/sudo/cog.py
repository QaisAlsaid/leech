from leech.core.cog.cog import Cog, CogContext
from leech.core.command.runtime import Command, Argument
from leech.core.command.model import CommandDescriptor
from leech.core.command.invocation import Invocation
from leech.core.command.binder import CommandBinder
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.provider.runtime import Provider
from leech.core.command.native.binding import NativeBinding
from leech.leech import Leech
from leech.core.permissions import is_owner
from leech.core.error import BaseError
from leech.core.command.slash.binding import SlashBinding
from leech.core.command.mock.binding import MockBinding
from leech.core.cog.error import CogError


class SudoCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)

    async def load(self):
        sudo = Command(
            name="sudo",
            description="privileged commands (owner only)",
            help_content="commands that are only allowed to be run by owner, regardless of the permissions system.",
            arguments=[],
            subcommands=self.get_subcommands(),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(sudo.name, sudo):
            raise CogError(f"failed to load '{sudo.name}' command")
        doas_desc = CommandDescriptor(
            name="doas",
            description=sudo.description,
            help_content=sudo.help_content,
            usage_example=sudo.usage_example,
            arguments=[],
            subcommands={},
            provider=ProviderDescriptor(type=ProviderType.SELF, target=[sudo.name]),
            is_root=sudo.is_root,
        )
        doas = self.context.client.binder.bind(doas_desc)
        if not self.context.add_command(doas.name, doas):
            raise CogError(f"failed to load '{doas.name}' self-command.")

    def get_subcommands(self) -> dict[str, Command]:
        return self.get_echo() | self.get_cog()

    def check_permissions(self, id: int):
        return is_owner(id)

    def get_cog(self) -> dict[str, Command]:
        def leaf(name: str, description: str, handler, arguments=None) -> Command:
            return Command(
                name=name,
                description=description,
                arguments=arguments or [],
                subcommands={},
                binding=NativeBinding(handler=handler),
            )

        path_arg = Argument(
            name="path",
            description="the cog module path, e.g. leech.cogs.ai.cog.",
            usage_example="path=leech.cogs.ai.cog",
            type=TypesRegistry.require(Types.STR),
            required=True,
        )

        return {
            "cog": Command(
                name="cog",
                description="load, unload and reload cogs.",
                usage_example=">sudo cog reload path=leech.cogs.ai.cog",
                arguments=[],
                binding=MockBinding(),
                subcommands={
                    "list": leaf("list", "list loaded cogs.", self.cog_list_handler),
                    "load": leaf(
                        "load", "load a cog.", self.cog_load_handler, [path_arg]
                    ),
                    "unload": leaf(
                        "unload", "unload a cog.", self.cog_unload_handler, [path_arg]
                    ),
                    "reload": leaf(
                        "reload",
                        "reload a cog (hot code swap).",
                        self.cog_reload_handler,
                        [path_arg],
                    ),
                },
            )
        }

    def _cogs(self):
        return self.context.client.cogs

    def _refuse_self(self, path: str, action: str) -> None:
        if path == self.context.owner:
            raise CogError(f"i can't {action} myself 😁", is_user_error=True)

    async def cog_list_handler(self, invocation: Invocation):
        paths = sorted(self._cogs().loaded)
        await invocation.message.reply(
            "\n".join(f"`{p}`" for p in paths) or "no cogs loaded."
        )

    async def cog_load_handler(self, invocation: Invocation):
        path = invocation.arguments["path"].strip()
        await self._cogs().load(path)  # CogError → friendly error reply via dispatch4
        await invocation.message.reply(f"loaded `{path}`.")

    async def cog_unload_handler(self, invocation: Invocation):
        path = invocation.arguments["path"].strip()
        self._refuse_self(path, "unload")
        if not await self._cogs().unload(path):
            raise CogError(f"cog '{path}' is not loaded.", is_user_error=True)
        await invocation.message.reply(f"unloaded `{path}`.")

    async def cog_reload_handler(self, invocation: Invocation):
        path = invocation.arguments["path"].strip()
        self._refuse_self(path, "reload")
        await self._cogs().reload(path)
        await invocation.message.reply(f"reloaded `{path}`.")

    async def echo_handler(self, invocation: Invocation):
        if not self.check_permissions(invocation.message.author.id):
            raise BaseError("blud thinks he's an owner 😭🥀")
        await invocation.message.reply(str(invocation.arguments["message"]))

    def get_echo(self) -> dict[str, Command]:
        echo = Command(
            name="echo",
            description="make me say something",
            usage_example=">sudo echo message=hi",
            help_content="make the bot say whatever message argument is.",
            arguments=[
                Argument(
                    name="message",
                    description="what to say.",
                    usage_example="message=hello word!",
                    help_content="what should the bot say.",
                    type=TypesRegistry.require(Types.STR),
                )
            ],
            subcommands={},
            binding=NativeBinding(handler=self.echo_handler),
        )
        return {echo.name: echo}


def create_cog(context: CogContext):
    return SudoCog(context=context)
