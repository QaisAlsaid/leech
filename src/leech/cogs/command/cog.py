from dataclasses import dataclass
from typing import Any

import discord
from leech.core.cog.cog import Cog, CogContext
from leech.core.cog.error import CogError
from leech.core.command.runtime import Command, Argument
from leech.core.command.model import CommandDescriptor
from leech.core.command.slash import adapter
from leech.core.parser.error import ParseError
from leech.core.parser.token import Token, TokenType
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.command.native.binding import NativeBinding
from leech.core.command.self.binding import SelfBinding
from leech.core.parser.types.types import IntType, UserType
from leech.core.command.invocation import Invocation
import leech.core.helpers.text.splitter as splitter

import leech.core.command.resolver as command_resolver
import leech.core.command.slash.invoker as invoker
import leech.core.command.slash.resolver as resolver
import leech.core.parser.lexer as lexer
import leech.core.parser.parser as parser
import leech.config as config
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.command.mock.binding import MockBinding

@dataclass
class ResolvedSlashInvocation:
    resolved: resolver.ResolvedSlashCommand
    arguments: list[Argument]
    parsed: dict[str, Any]


@dataclass
class InvokeAlias:
    name: str
    type: ProviderType
    bot: int
    target: list[str]
    arguments: list[Argument]
    defaults: dict[str, Any]


def bot(bot: Token) -> int:
    if bot.type != TokenType.USER:
        if bot.type == TokenType.RAW:
            try:
                return int(bot.raw)
            except ValueError as e:
                raise ParseError(
                    f"i expected argument 'bot' to be a valid bot id or mention."
                ) from e
    raise ParseError(f"i expected argument 'bot' to be a valid bot id or mention.")

def format_option(option: discord.Option) -> str:
    type_name = option.type.name

    required = "required" if option.required else "optional"

    return f"    {option.name}: " f"{type_name} ({required})"


def format_arguments(command) -> list[str]:
    arguments = []

    for option in command.options:
        if option.type in (
            discord.ApplicationCommandOptionType.subcommand,
            discord.ApplicationCommandOptionType.subcommand_group,
        ):
            continue

        arguments.append(format_option(option))

    return arguments


def format_slash_command(
    command: discord.SlashCommand | discord.SubCommand,
    path: str,
    indent: str = "",
) -> list[str]:
    lines = []

    invocable = not command.is_group()

    lines.append(f"{indent}{path}")
    lines.append(f"{indent}  {command.description}")
    lines.append(f"{indent}  invocable: {'yes' if invocable else 'no'}")

    arguments = format_arguments(command)
    if arguments:
        lines.append(f"{indent}  arguments:")
        lines.extend(f"{indent}{line}" for line in arguments)

    for child in command.children:
        child_path = f"{path} {child.name}"

        lines.append("")
        lines.extend(
            format_slash_command(
                child,
                child_path,
                indent + "  ",
            )
        )

    return lines

def format_template(template: InvokeAlias) -> str:
    lines = [
        f"name: {template.name}",
        f"type: {template.type.value}",
        f"bot: {template.bot}",
        f"target: /{' '.join(template.target)}",
    ]

    if template.arguments:
        lines.append("arguments:")
        for arg in template.arguments:
            required = "required" if arg.required else "optional"
            lines.append(
                f"  {arg.name}: {arg.type.type.name} ({required})"
            )
    else:
        lines.append("arguments: none")

    if template.defaults:
        lines.append("defaults:")
        for name, value in template.defaults.items():
            lines.append(f"  {name}: {value!r}")
    else:
        lines.append("defaults: none")

    return "\n".join(lines)

class CommandCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        # self.add_wizard = AddWizard(context=context)
        self.context.add_on_load(self.load)
        self.invoke_aliases: dict[str, InvokeAlias] = {}

    async def load(self):
        command = Command(
            name="command",
            description="configure custom commands/aliases.",
            arguments=[],
            subcommands=self.get_subcommands(),
            binding=MockBinding(),
            is_root=True
        )

        if not self.context.add_command(command.name, command):
            raise CogError(f"i failed to load '{command.name}' command.")
        cmd_desc = CommandDescriptor(
            name="cmd",
            description=command.description,
            usage_example=command.usage_example,
            help_content=command.help_content,
            arguments=[],
            subcommands={},
            provider=ProviderDescriptor(type=ProviderType.SELF, target=[command.name]),
            is_root=command.is_root
        )
        cmd = self.context.client.binder.bind(cmd_desc)
        if not self.context.add_command(cmd.name, cmd):
            raise CogError(f"i failed to load '{cmd.name}' self-command.")

    def get_subcommands(self) -> dict[str, Command]:
        return {
            **self.get_add(),
            **self.get_invoke(),
            **self.get_introspect(),
        }

    def get_add(self) -> dict[str, Command]:
        return {
            "add": Command(
                name="add",
                description="add custom command/alias via an interactive wizard. (not implemented)",
                arguments=[
                    Argument(
                        name="global",
                        description="owner only option to make the added custom command/alias global",
                        type=TypesRegistry.require(Types.BOOL),
                        required=False,
                    )
                ],
                subcommands={},
                binding=NativeBinding(handler=self.add_handler),
            )
        }

    def get_invoke_template(self) -> dict[str, Command]:
        return {
            "template": Command(
                name="template",
                description="create a template for on-the-fly invocation.",
                usage_example=">cmd invoke template img slash @fergun img google multi-images=true",
                help_content="templates allows you to setup template for on-the-fly invocation by letting you set up default arguments and target command\nsuppose you want to invoke a salsh command for @fergun where the command is called 'img google' and it takes 2 arguments the first one is called 'multi-images' and is not required, and the second is called 'query' and is required, so using the traditional on-the-fly invocation syntax it would look something like: ``>cmd invoke slash @bot img google multi-images=off bird nice``.\nbut you want this to be less mouthful per say, so you set-up a template for the things you know won't chaneg like the target bot (@fergun) and invocation type (slash) and the command itself (img google) and the multi-images parameter, so by setting up a templte as the example shows, you turn the whole thing into just ``>img bird nice`` how convenient!",
                arguments=[
                    Argument(
                        name="command",
                        description="the command name that is used to invoke the template.",
                        usage_example="command=ping",
                        help_content="the target command that you wish to template, it can be leech own command, or other bot's prefix or slash command (depending on the type argument).",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="type",
                        description="the command type that will to be templated (native, self, prefix, slash).",
                        usage_example="type=slash",
                        help_content="self types represents commands that are declared via the self aliasing facade, natives are for bot native commands, prefix for other bot's prefixes, and slash are for other bot's slash.",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="bot",
                        description="the target bot.",
                        usage_example="bot=@fergun",
                        help_content="the bot that this template targets, it is only relevant for slash commands, where it is required to match leech's own id in self or native commands, and is rejected for prefix commands.",
                        type=TypesRegistry.require(Types.USER),
                        required=False,
                    ),
                    Argument(
                        name="invocation",
                        description="the actual invocation to be templated.",
                        usage_example="invocation=img google multi-images=true",
                        help_content="invocations are just like how you would invoke another command with '>', but also they work for other bot's slash commands or prefix commands (depending on the 'type' argument), as long as the target arguments can be resolved (positionaly or by name (see ``>help concepts arguments`` for more on that)).\nfor templates case an invocation isn't required to be complete at declaration time, where you can provide partial invocation (i.e. template) that it will be permissively validated, then the complete validation happens at runtime.\nthis allows for interesting simplified usages while staying powerful.",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                ],
                subcommands={},
                binding=NativeBinding(handler=self.invoke_template_handler),
            )
        }

    def get_invoke(self) -> dict[str, Command]:
        return {
            "invoke": Command(
                name="invoke",
                description="invoke a commad type on the fly.",
                usage_example=">cmd invoke slash @fergun img google multi-images=true query=bird nice",
                help_content="invoke lets you supply a string of text, and it will turn it into real leech command invocation on-the-fly.\nthis allows for running quick commands without actually registering them it supports all command types.",
                arguments=[
                    Argument(
                        name="type",
                        description="the command type that will to be invoked (native, self, prefix, slash).",
                        usage_example="type=slash",
                        help_content="self types represents commands that are declared via the self aliasing facade, natives are for bot native commands, prefix for other bot's prefixes, and slash are for other bot's slash.",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="bot",
                        description="the target bot.",
                        usage_example="bot=@fergun",
                        help_content="the bot that this invocation targets, it is only relevant for slash commands, where it is required to match leech's own id in self or native commands, and is rejected for prefix commands.",
                        type=TypesRegistry.require(Types.USER),
                        required=False,
                    ),
                    Argument(
                        name="invocation",
                        description="the actual command invocation.",
                        usage_example="invocation=img google multi-images=true query=bird nice",
                        help_content="invocations are just like how you would invoke another command with '>', but also they work for other bot's slash commands or prefix commands (depending on the 'type' argument), as long as the target arguments can be resolved (positionaly or by name (see ``>help concepts arguments`` for more on that)).",

                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                ],
                subcommands={**self.get_invoke_template()},
                binding=NativeBinding(handler=self.invoke_handler),
            )
        }

    def get_introspect(self) -> dict[str, Command]:
        return {
            "introspect": Command(
                name="introspect",
                description="intorspect available commands for given type.",
                usage_example=">cmd introspect slash @fergun",
                help_content="it shows all registered commands for given type, and can be matched by specific bot.",
                arguments=[
                    Argument(
                        name="type",
                        description="the command type to introspect (native, self, prefix, slash).",
                        help_content="self types represents commands that are declared via the self aliasing facade, natives are for bot native commands, prefix for other bot's prefixes, and slash are for other bot's slash.",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="bot",
                        description="the target bot",
                        usage_example="bot=@fergun",
                        help_content="the bot to introspect its commans, it is only relevant for slash commands, where it is required to match leech's own id in self or native commands, and is rejected for prefix commands.",
                        type=TypesRegistry.require(Types.USER),
                        required=False,
                    ),
                ],
                subcommands={},
                binding=NativeBinding(handler=self.introspect_handler),
            )
        }

    async def add_handler(self, invocation: Invocation):
        raise CogError("unsupported command at the moment")
        # await self.add_wizard.start(message=invocation.message, is_global=bool(invocation.arguments[0].value))

    async def invoke_handler(self, invocation: Invocation):
        invk_type = str(invocation.arguments["type"]).lower()
        raw_invocation = invocation.arguments["invocation"]
        bot = invocation.arguments.get("bot")
        match invk_type:
            case "slash":
                return await self.invoke_slash(invocation, bot, raw_invocation)
            case "self":
                return await self.invoke_self(invocation, bot, raw_invocation)
            case "prefix":
                return await self.invoke_prefix(invocation, bot, raw_invocation)
            case _:
                raise CogError(
                    f"invalid command type: '{invk_type}'.", is_user_error=True
                )

    async def introspect_handler(self, invocation: Invocation):
        type = invocation.arguments["type"]
        bot = invocation.arguments.get("bot")
        match type:
            case "slash":
                introspection = await self.introspect_slash(invocation, bot)
            case "self":
                introspection = await self.introspect_self(invocation, bot)
            case "prefix":
                raise CogError(f"it is impossible to introspect prefix commands.", is_user_error=True)
            case 'native':
                introspection = await self.introspect_native(invocation, bot)
            case _:
                raise CogError(
                    f"invalid introspection type: '{type}'.", is_user_error=True
                )

        if not introspection:
            return

        msg = invocation.message
        for chunk in splitter.split(introspection):
            msg = await msg.reply(chunk)

    async def invoke_template_handler(self, invocation: Invocation):
        name = invocation.arguments["command"]
        try:
            template_type = ProviderType(str(invocation.arguments["type"]).lower())
        except ValueError as e:
            raise CogError(
                "you have provided an invalid template type.", is_user_error=True
            ) from e

        bot = invocation.arguments.get("bot")
        raw_invocation = invocation.arguments["invocation"]
        match template_type:
            case ProviderType.SLASH:
                pass
            case _:
                raise CogError("unsupported template type at the moment.")

        if bot is None:
            raise CogError(
                "'bot' argument is required for slash templates.", is_user_error=True
            )

        result = await self.resolve_slash_invocation(
            message=invocation.message,
            bot=bot,
            raw_invocation=raw_invocation,
            check_missing=False,
        )
        template = InvokeAlias(
            name=name,
            type=template_type,
            bot=bot,
            target=result.resolved.target,
            arguments=result.arguments,
            defaults=result.parsed,
        )
        command = Command(
            name=template.name,
            description=f"**generated** template for /{' '.join(template.target)}",
            arguments=template.arguments,
            subcommands={},
            binding=NativeBinding(handler=self.invoke_template_trampoline_handler),
            defaults=template.defaults,
        )
        if not self.context.add_command(command.name, command):
            raise CogError(
                f"i failed to register command: '{command.name}' as it is already registered (think of a unique name perhaps 😁).",
                is_user_error=True,
            )
        self.invoke_aliases[template.name] = template
        await invocation.message.reply(f"i added command: `{template.name}`, with template: ```yaml\n{format_template(template)}\n```")

    async def invoke_template_trampoline_handler(self, invocation: Invocation):
        template = self.invoke_aliases[invocation.command.name]
        match template.type:
            case ProviderType.SLASH:
                resolved = await resolver.resolve_target(
                    invocation.message.channel,
                    template.target,
                    template.bot,
                )

                await invoker.invoke(
                    resolved.command,
                    invocation.message.channel,
                    invocation.arguments,
                )

            case _:
                raise CogError("unsupported template type at the moment.")

    async def resolve_slash_invocation(
        self,
        message: discord.Message,
        bot: int,
        raw_invocation: str,
        *,
        check_missing: bool = True,
    ) -> ResolvedSlashInvocation:
        lexed = lexer.lex(raw_invocation, prefixes=[])

        if not lexed.tokens:
            raise ParseError(
                f"i was unable to parse raw invocation: '{raw_invocation}'."
            )

        resolved = await resolver.resolve(
            message.channel,
            lexed.tokens,
            bot,
        )

        arguments = [
            arg
            for opt in resolved.command.options
            if (arg := adapter.argument_from_option(opt, permissive=True)) is not None
        ]

        parsed = parser.parse_args(
            parser.ParseContext(
                arguments=arguments,
                tokens=resolved.args,
            ),
            check_missing=check_missing,
        )
        return ResolvedSlashInvocation(
            resolved=resolved, arguments=arguments, parsed=parsed
        )

    async def invoke_slash(
        self, invocation: Invocation, bot: int | None, raw_invocation: str
    ):
        if bot is None:
            raise CogError(
                "'bot' argument is required for slash invocation.", is_user_error=True
            )
        result = await self.resolve_slash_invocation(
            message=invocation.message, bot=bot, raw_invocation=raw_invocation
        )
        await invoker.invoke(
            result.resolved.command, invocation.message.channel, result.parsed
        )

    async def invoke_self(
        self, invocation: Invocation, bot: int | None, raw_invocation: str
    ):
        if bot is not None and bot != self.context.client.user.id:
            raise CogError(
                "'bot' argument should match my user id in self invocation (or better leave it blank 😁).",
                is_user_error=True,
            )
        lexed = lexer.lex(raw_invocation, prefixes=[])
        if not lexed:
            raise CogError(f"'invocation' is empty, i can't do anything with it.")
        if (
            lexed.tokens[0].type != TokenType.RAW
            or not lexed.tokens[0].raw.isidentifier()
        ):
            raise CogError(
                f"you have provided an invalid command identifier: '{lexed.tokens[0]}'",
                is_user_error=True,
            )
        resolved = command_resolver.resolve_command(
            lexed.tokens, self.context.client.commands
        )
        if resolved is None:
            raise CogError(
                f"you have provided an invalid command\nmaybe try ``>help``?",
                is_user_error=True,
            )
        arguments = parser.parse_args(
            parser.ParseContext(
                arguments=resolved.command.arguments, tokens=resolved.args
            )
        )
        return Invocation(invocation.message, resolved.command, arguments)

    async def invoke_prefix(
        self, invocation: Invocation, bot: int | None, raw_invocation: str
    ):
        #TODO harden!
        if len(raw_invocation) > splitter.DISCORD_MESSAGE_LIMIT:
            raise CogError(f"prefix command too long. (limit is {splitter.DISCORD_MESSAGE_LIMIT}).")
        await invocation.message.channel.send(f"{raw_invocation}")

    async def introspect_slash(self, invocation: Invocation, bot: int | None):
        commands = None
        try:
            commands = await invocation.message.channel.application_commands()
        except TypeError as e:
            raise CogError(
                f"i failed to fetch application commands, details: {e}",
                is_user_error=True,
            ) from e

        lines = []
        for command in commands:
            if (
                not isinstance(command, discord.SlashCommand)
                or bot is not None
                and command.application_id != bot
            ):
                continue

            lines.append(f"/{command.name}")
            lines.append(f" {command.description}")
            lines.append(f" invocable: {'no' if command.is_group() else 'yes'}")
            arguments = format_arguments(command)
            if arguments:
                lines.append(" arguments:")
                lines.extend(arguments)

            for child in command.children:
                lines.append("")
                lines.extend(
                    format_slash_command(child, f"{command.name} {child.name}", " ")
                )

            lines.append("")

        return "\n".join(lines).rstrip()

    async def introspect_self(self, invocation: Invocation, bot: int | None):
        if bot is not None and bot != self.context.client.user.id:
            raise CogError(
                "'bot' argument should match my user id in self introspection (or better leave it blank 😁).",
                is_user_error=True,
            )
        introspect = ""
        for command in self.context.client.commands.registered_commands.values():
            if not isinstance(command.binding, SelfBinding):
                continue

            introspect += f"command: '{command.name}', description: '{command.description}', arguments: '{command.arguments}'\n"

        return introspect

    async def introspect_native(self, invocation: Invocation, bot: int | None):
        if bot is not None and bot != self.context.client.user.id:
            raise CogError(
                "'bot' argument should match my user id in native introspection (or better leave it blank 😁).",
                is_user_error=True,
            )
        introspect = ""
        for command in self.context.client.commands.registered_commands.values():
            if isinstance(command.binding, SelfBinding):
                continue

            introspect += f"command: '{command.name}', description: '{command.description}', arguments: '{command.arguments}'\n"

        return introspect


def create_cog(context: CogContext):
    return CommandCog(context=context)
