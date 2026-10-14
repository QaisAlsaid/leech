from leech.core.cog.cog import Cog, CogContext
from leech.core.command.sudo.binding import SudoBinding
from leech.core.command.runtime import Command, Argument
from leech.core.command.model import CommandDescriptor
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.command.native.binding import NativeBinding
from leech.core.command.mock.binding import MockBinding
from leech.core.cog.error import CogError
from leech.core.permissions import is_owner


class SudoCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)

    async def load(self):
        # the modifier itself
        sudo = Command(
            name="sudo",
            description="run a command with elevation (owner only).",
            usage_example=">sudo cog reload path=leech.cogs.ai.cog",
            help_content=(
                "sudo is a modifier, not a command family: it takes any command "
                "elevates it then executes. only the owner may use it, and the "
                "assistant never can. commands that require elevation will not run without sudo."
            ),
            arguments=[
                Argument(
                    name="command",
                    description="the command line to run elevated.",
                    usage_example="command=cog reload path=leech.cogs.ai.cog",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                )
            ],
            subcommands={},
            binding=SudoBinding(self.context.client.commands),
            is_root=True,
        )
        if not self.context.add_command(sudo.name, sudo):
            raise CogError(f"failed to load '{sudo.name}' command")

        doas = self.context.client.binder.bind(
            CommandDescriptor(
                name="doas",
                description=sudo.description,
                help_content=sudo.help_content,
                usage_example=sudo.usage_example,
                arguments=[],
                subcommands={},
                provider=ProviderDescriptor(type=ProviderType.SELF, target=[sudo.name]),
                is_root=True,
            )
        )
        if not self.context.add_command(doas.name, doas):
            raise CogError(f"failed to load '{doas.name}' self-command.")

        # the old sudo toolbox, now standalone roots — elevated where destructive
        for name, command in (self.get_cog() | self.get_echo()).items():
            if not self.context.add_command(name, command):
                raise CogError(f"failed to load '{name}' command")

    def get_cog(self) -> dict[str, Command]:
        def leaf(name, description, handler, arguments=None, elevated=False):
            return Command(
                name=name,
                description=description,
                arguments=arguments or [],
                subcommands={},
                binding=NativeBinding(handler=handler),
                elevated=elevated,
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
                is_root=True,
                subcommands={
                    "list": leaf("list", "list loaded cogs.", self.cog_list_handler),
                    "load": leaf(
                        "load",
                        "load a cog.",
                        self.cog_load_handler,
                        [path_arg],
                        elevated=True,
                    ),
                    "unload": leaf(
                        "unload",
                        "unload a cog.",
                        self.cog_unload_handler,
                        [path_arg],
                        elevated=True,
                    ),
                    "reload": leaf(
                        "reload",
                        "reload a cog (hot code swap).",
                        self.cog_reload_handler,
                        [path_arg],
                        elevated=True,
                    ),
                },
            )
        }

    def get_echo(self) -> dict[str, Command]:
        return {
            "echo": Command(
                name="echo",
                description="make me say something (owner only).",
                usage_example=">echo message=hi",
                help_content="make the bot say whatever the message argument is.",
                arguments=[
                    Argument(
                        name="message",
                        description="what to say.",
                        usage_example="message=hello word!",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    )
                ],
                subcommands={},
                binding=NativeBinding(handler=self.echo_handler),
                is_root=True,
            )
        }

    def _cogs(self):
        return self.context.client.cogs

    def _refuse_self(self, path: str, action: str) -> None:
        if path == self.context.owner:
            raise CogError(
                f"i can't {action} myself 😁 (do it from another cog).",
                is_user_error=True,
            )

    async def cog_list_handler(self, invocation: Invocation):
        paths = sorted(self._cogs().loaded)
        await invocation.message.reply(
            "\n".join(f"`{p}`" for p in paths) or "no cogs loaded."
        )

    async def cog_load_handler(self, invocation: Invocation):
        path = invocation.arguments["path"].strip()
        await self._cogs().load(path)
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
        await invocation.message.reply(str(invocation.arguments["message"]))


def create_cog(context: CogContext):
    return SudoCog(context=context)
