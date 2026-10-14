from leech.core.cog.cog import Cog, CogContext
from leech.core.command.binding import Binding
from leech.core.command.runtime import Command, Argument
from leech.core.command.model import CommandDescriptor
from leech.core.command.invocation import Invocation
from leech.core.command.binder import CommandBinder
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.provider.runtime import Provider
from leech.core.command.native.binding import NativeBinding
from leech.core.command.self.binding import SelfBinding
from leech.core.command.slash.binding import SlashBinding
from leech.leech import Leech
from leech.core.permissions import is_owner
from leech.core.cog.error import CogError
from leech.core.command.mock.binding import MockBinding
from leech.core.command.helpers import is_invocable
import leech.core.parser.lexer as lexer
import leech.core.helpers.text.splitter as splitter
import leech.core.command.resolver as resolver


def format_root_commands(commands: dict[str, Command]) -> str:
    sections = []

    for path, command in commands.items():
        if not command.is_root:
            continue

        sections.append(format_command_summary(command, path))

    if not sections:
        return "no commands available."

    return "# commands\n\n" + "\n\n".join(sections)


def format_command_type(binding: Binding) -> str:
    if isinstance(binding, NativeBinding):
        kind = "native"
    elif isinstance(binding, SelfBinding):
        kind = "self"
    elif isinstance(binding, SlashBinding):
        kind = "slash"
    elif isinstance(binding, MockBinding):
        kind = "group"
    else:
        kind = "unknown"

    return f"**type:** `{kind}`, invocable: `{is_invocable(binding)}`"


def format_command_summary(command: Command, path: str) -> str:
    lines = [
        f"`{' '.join(path.split('.'))}` - {f'alias to `{command.binding.target.name}` - ' if isinstance(command.binding, SelfBinding) else ''} {command.description}"
    ]

    if command.usage_example:
        lines.append(f"example: `{command.usage_example}`")

    lines.extend([format_command_type(command.binding)])

    if command.arguments:
        arguments = ", ".join(
            f"`{arg.name}: {arg.type.type.name}`"
            + ("" if arg.required else " (optional)")
            for arg in command.arguments
        )
        lines.append(f"arguments: {arguments}")

    return "\n".join(lines)


def format_command_details(command: Command, path: str) -> str:
    lines = [
        f"# `{' '.join(path.split('.'))}` {f'alias to `{command.binding.target.name}`' if isinstance(command.binding, SelfBinding) else ''}",
    ]
    lines.extend(
        [
            "",
            "**description**",
            command.description,
        ]
    )

    if command.usage_example:
        lines.extend([f"**example:** `{command.usage_example}`"])
    if command.help_content:
        lines.extend(["**additional help**", command.help_content])

    lines.extend(["", "**meta**"])
    lines.extend([format_command_type(command.binding)])
    if command.arguments:
        lines.extend(["**arguments**"])
    for position, arg in enumerate(command.arguments, start=1):
        required = "required" if arg.required else "optional"
        lines.append(f"- `{arg.name}` — `{arg.type.type.name}` ({required})")

        if arg.description:
            lines.append(f"  {arg.description}")

        if arg.name in command.defaults:
            lines.append(f"  default: `{command.defaults[arg.name]}`")

        if arg.usage_example:
            lines.append(f"  example: `{arg.usage_example}`")

    if command.subcommands:
        lines.extend(["", "## subcommands"])

        for child in command.subcommands.values():
            child_path = f"{path} {child.name}"
            lines.append(format_command_summary(child, child_path))
            lines.append("\n")

    return "\n".join(lines)


def format_argument_details(command: Command, argument: Argument, path: str) -> str:
    try:
        position = next(
            i for i, arg in enumerate(command.arguments, start=1) if arg is argument
        )
    except StopIteration:
        position = None

    required = "yes" if argument.required else "no"
    lines = [
        f"# argument `{argument.name}`",
        f"**meta**",
        f"command: `{' '.join(path.split('.'))}`",
    ]

    if position is not None:
        lines.append(f"declaration position: {position}")

    lines.extend(
        [
            f"type: `{argument.type.type.name}`",
            f"required: {required}",
        ]
    )

    if argument.name in command.defaults:
        lines.append(f"default: `{command.defaults[argument.name]}`")
    elif not argument.required:
        lines.append("default: not specified")

    if argument.description:
        lines.extend(["", "**description**", argument.description])

    if argument.usage_example:
        lines.extend([f"**example:** `{argument.usage_example}`"])

    if argument.help_content:
        lines.extend(["**additional help**", argument.help_content])

    if argument.type.description:
        lines.extend(
            [
                "",
                f"**type information**",
                f"**{argument.type.type}**: {argument.type.description}",
            ]
        )

    if argument.type.usage_example:
        lines.append(f"type example: `{argument.type.usage_example}`")

    return "\n".join(lines)


class HelpCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)

    async def load(self):
        help = Command(
            name="help",
            description="get help.",
            usage_example=">help",
            help_content="get help for all root commands or specific command path.",
            arguments=[
                Argument(
                    name="path",
                    description="the command path to get help for.",
                    usage_example="path=cmd invoke",
                    help_content="you can either leave blank to get help message for all root commands.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                )
            ],
            subcommands=self.get_subcommands(),
            binding=NativeBinding(self.help_handler),
            is_root=True,
        )
        if not self.context.add_command(help.name, help):
            raise CogError(f"failed to load '{help.name}' command.")
        sos_desc = CommandDescriptor(
            name="sos",
            description=help.description,
            arguments=[],
            subcommands={},
            provider=ProviderDescriptor(type=ProviderType.SELF, target=["help"]),
            is_root=help.is_root,
        )
        sos = self.context.client.binder.bind(sos_desc)
        if not self.context.add_command(sos.name, sos):
            raise CogError(f"failed to load '{sos.name}' self-command.")

    def get_subcommands(self) -> dict[str, Command]:
        return {**self.get_concepts()}

    def get_concepts(self) -> dict[str, Command]:
        return {
            "concepts": Command(
                name="concepts",
                description="get help regarding leech specific concepts",
                usage_example=">help concepts concept=types",
                help_content="concepts are leech specific stuff that can affect your usage experience.",
                arguments=[
                    Argument(
                        name="concept",
                        description="the concept you wish to get help for.",
                        usage_example="concept=types",
                        help_content="you can leave blank to get list of all avilable concepts.",
                        type=TypesRegistry.require(Types.STR),
                        required=False,
                    )
                ],
                subcommands={},
                binding=NativeBinding(self.concepts_handler),
            )
        }

    async def help_handler(self, invocation: Invocation):
        raw_path = invocation.arguments.get("path") or ""

        if not raw_path.strip():
            text = format_root_commands(
                self.context.client.commands.registered_commands
            )
        else:
            lexed = lexer.lex(raw_path, prefixes=[])
            resolved = resolver.resolve_command(
                lexed.tokens, self.context.client.commands
            )

            if resolved is None:
                raise CogError(
                    f"command path: {raw_path} not found.\n(try ``>help``)",
                    is_user_error=True,
                )
            else:
                remaining = resolved.args

                if not remaining:
                    text = format_command_details(resolved.command, resolved.path)
                else:
                    arguments_by_name = {
                        arg.name: arg for arg in resolved.command.arguments
                    }

                    requested_names = [token.raw for token in remaining]

                    if len(requested_names) != len(set(requested_names)):
                        raise CogError(
                            "the same argument was requested more than once.",
                            is_user_error=True,
                        )

                    missing = [
                        name
                        for name in requested_names
                        if name not in arguments_by_name
                    ]

                    if missing:
                        raise CogError(
                            f"unknown argument(s): {', '.join(missing)}.",
                            is_user_error=True,
                        )

                    pages = [
                        format_argument_details(
                            resolved.command, arguments_by_name[name], resolved.path
                        )
                        for name in requested_names
                    ]

                    text = "\n\n---\n\n".join(pages)

        message = invocation.message
        for chunk in splitter.split(text):
            message = await message.reply(chunk)

    async def concepts_handler(self, invocation: Invocation):
        raise CogError("concepts are not implemented at the moment.")


def create_cog(context: CogContext):
    return HelpCog(context=context)
