from dataclasses import dataclass, field
from typing import Any, Optional

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
from leech.core.helpers.attachments.attachments import gather_attachments
from leech.core.command.slash.execution import execute_slash, execute_prefix
import leech.core.command.resolver as command_resolver
from leech.core.command.registry import is_valid_identifier
import leech.core.command.slash.resolver as resolver
import leech.core.parser.lexer as lexer
import leech.core.parser.parser as parser
import leech.config as config
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.command.mock.binding import MockBinding
from leech.core.command.helpers import is_invocable
from leech.core.helpers.interactions.model import (
    EchoSpec,
    HarvestedAttachment,
    HarvestedComponent,
    HarvestedMessage,
    harvest_full,
    media_name,
)
from leech.core.command.slash.press import PressTarget, press_and_collect
import json
import os
import logging
from pathlib import Path
import asyncio
import io
import httpx
import discord
import time
from leech.core.cog.context import Priority
from leech.core.permissions import is_owner

PRESS_TTL = 15 * 60.0
TEMPLATES_STORE = Path("persistent/v1/commands/templates.json")
ECHO_MEDIA_CAP = 4
ECHO_FILE_MAX_BYTES = 20 * 1024 * 1024

_logger = logging.getLogger(__name__)


@dataclass
class ResolvedSlashInvocation:
    resolved: resolver.ResolvedSlashCommand
    arguments: list[Argument]
    parsed: dict[str, Any]


@dataclass
class InvokeAlias:
    name: str
    type: ProviderType
    bot: Optional[int]
    target: list[str]
    arguments: list[Argument]
    defaults: dict[str, Any]
    channel: Optional[int] = None
    echo: Optional[str] = None
    ai: bool = False


@dataclass
class PressEntry:
    original: HarvestedMessage
    echo: Optional[discord.Message]
    buttons: list[HarvestedComponent]
    requester_id: int
    expires: float
    spec: EchoSpec = field(default_factory=EchoSpec)

    def target_for(self, component: HarvestedComponent) -> PressTarget:
        return PressTarget.of(self.original, component)


def _press_line(buttons: list[HarvestedComponent]) -> str:
    names = " ".join(
        f"[{i} {(b.label or b.emoji or b.custom_id or '?').strip(':')}]"
        for i, b in enumerate(buttons, start=1)
    )
    return f"press: {names} (reply to this message with a number)"


def template_to_dict(template: InvokeAlias) -> dict:
    return {
        "name": template.name,
        "type": template.type.value,
        "bot": template.bot,
        "target": list(template.target),
        "arguments": [
            {
                "name": a.name,
                "type": a.type.type.value,
                "description": a.description,
                "required": a.required,
                "usage_example": a.usage_example,
                "help_content": a.help_content,
            }
            for a in template.arguments
        ],
        "defaults": {
            name: (getattr(value, "url", value) if hasattr(value, "url") else value)
            for name, value in template.defaults.items()
        },
        "channel": template.channel,
        "echo": template.echo,
        "ai": template.ai,
    }


def template_from_dict(data: dict) -> InvokeAlias:
    return InvokeAlias(
        name=data["name"],
        type=ProviderType(data["type"]),
        bot=int(data["bot"]) if data.get("bot") is not None else None,
        target=list(data.get("target", [])),
        arguments=[
            Argument(
                name=arg["name"],
                type=TypesRegistry.require(Types(arg["type"])),
                description=arg.get("description", ""),
                required=arg.get("required", True),
                usage_example=arg.get("usage_example", ""),
                help_content=arg.get("help_content", ""),
            )
            for arg in data.get("arguments", [])
        ],
        defaults=dict(data.get("defaults", {})),
        channel=data.get("channel"),
        echo=data.get("echo"),
        ai=data.get("ai", False),
    )


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
    kind = template.type.value
    prefix = {"slash": "/", "prefix": "!", "self": ">"}.get(kind, "")
    lines = [f"name: {template.name}", f"type: {kind}"]
    if template.bot is not None:
        lines.append(f"bot: {template.bot}")
    lines.append(f"target: {prefix}{' '.join(template.target)}")

    if template.arguments:
        lines.append("arguments:")
        for arg in template.arguments:
            required = "required" if arg.required else "optional"
            lines.append(f"  {arg.name}: {arg.type.type.name} ({required})")
    else:
        lines.append("arguments: none")

    if template.defaults:
        lines.append("defaults:")
        for name, value in template.defaults.items():
            lines.append(f"  {name}: {value!r}")
    else:
        lines.append("defaults: none")

    if template.channel is not None:
        lines.append(f"channel: <#{template.channel}>")
    if template.echo is not None:
        lines.append(f"echo: {template.echo}")
    lines.append(f"ai: {'yes' if template.ai else 'no'}")

    return "\n".join(lines)


class CommandCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.invoke_aliases: dict[str, InvokeAlias] = {}
        self._press_entries: dict[int, PressEntry] = {}
        self.context.add_on_message(Priority.HIGH, self.on_press_reply)
        self._template_groups: set[str] = set()

    async def load(self):
        command = Command(
            name="command",
            description="configure custom commands/aliases.",
            arguments=[],
            subcommands=self.get_subcommands(),
            binding=MockBinding(),
            is_root=True,
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
            is_root=command.is_root,
        )
        cmd = self.context.client.binder.bind(cmd_desc)
        if not self.context.add_command(cmd.name, cmd):
            raise CogError(f"i failed to load '{cmd.name}' self-command.")
        await self._load_templates()

    def get_subcommands(self) -> dict[str, Command]:
        return {
            **self.get_invoke(),
            **self.get_introspect(),
            **self.get_press(),
            **self.get_reap(),
        }

    def get_press(self) -> dict[str, Command]:
        return {
            "press": Command(
                name="press",
                description="press a button on a bot's message.",
                usage_example="(reply to the message) >cmd press button=2",
                help_content=(
                    "reply to a message with buttons and press one by index "
                    "(1 based), label, or custom id. or pass message=<id> instead "
                    "of replying."
                ),
                arguments=[
                    Argument(
                        name="button",
                        description="index, label, or custom id.",
                        usage_example="button=2",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="message",
                        description="the message id (default: the replied to message).",
                        usage_example="message=1560117814012940342",
                        type=TypesRegistry.require(Types.INT),
                        required=False,
                    ),
                ],
                subcommands={},
                binding=NativeBinding(handler=self.press_handler),
            )
        }

    async def press_handler(self, invocation: Invocation):
        selector = str(invocation.arguments["button"]).strip()
        message_id = invocation.arguments.get("message")

        if message_id is None:
            ref = invocation.message.reference
            if ref is None or ref.message_id is None:
                raise CogError(
                    "reply to a message with buttons, or to my echo of one, "
                    "or pass message=<id>.",
                    is_user_error=True,
                )
            message_id = ref.message_id

        entry = self._press_entries.get(message_id)
        if entry is not None:
            if time.monotonic() >= entry.expires:
                self._press_entries.pop(message_id, None)
            else:
                component = self._find_button_in(entry.buttons, selector)
                if component is None:
                    raise CogError(
                        f"no button '{selector}' on that message.", is_user_error=True
                    )
                try:
                    result = await press_and_collect(
                        self.context.client, entry.target_for(component)
                    )
                except Exception as e:
                    raise CogError(f"the press failed: {e}", is_user_error=True) from e
                await self._apply_press_result(invocation.message, entry, result)
                return None

        try:
            msg = await invocation.message.channel.fetch_message(message_id)
        except discord.HTTPException as e:
            raise CogError(f"couldn't fetch that message: {e}", is_user_error=True)

        harvested = await harvest_full(msg, self.context.client)
        component = self._find_button_in(
            [c for c in harvested.components if c.kind == "button"], selector
        )
        if component is None:
            raise CogError(
                f"no button '{selector}' on that message.", is_user_error=True
            )
        try:
            result = await press_and_collect(
                self.context.client, PressTarget.of(harvested, component), message=msg
            )
        except Exception as e:
            raise CogError(f"the press failed: {e}", is_user_error=True) from e

        transient = PressEntry(
            original=harvested,
            echo=None,
            buttons=[
                c for c in harvested.components if c.kind == "button" and not c.disabled
            ],
            requester_id=invocation.message.author.id,
            expires=time.monotonic() + PRESS_TTL,
        )
        await self._apply_press_result(invocation.message, transient, result)
        return None

    def _register_pressable(
        self,
        echo: Optional[discord.Message],
        harvested: HarvestedMessage,
        requester_id: int,
        spec: Optional[EchoSpec] = None,
    ) -> Optional[PressEntry]:
        buttons = [
            c for c in harvested.components if c.kind == "button" and not c.disabled
        ]
        if not buttons:
            return None
        entry = PressEntry(
            original=harvested,
            echo=echo,
            buttons=buttons,
            requester_id=requester_id,
            expires=time.monotonic() + PRESS_TTL,
            spec=spec or EchoSpec(),
        )
        if echo is not None:
            self._press_entries[echo.id] = entry
        self._press_entries[harvested.message_id] = entry
        return entry

    def _find_button_in(self, buttons: list[HarvestedComponent], selector: str):
        if selector.isdigit():
            index = int(selector)
            if 1 <= index <= len(buttons):
                return buttons[index - 1]
        for c in buttons:
            if (
                c.label and c.label.lower() == selector.lower()
            ) or c.custom_id == selector:
                return c
        return None

    async def on_press_reply(self, message: discord.Message):
        if message.author.bot:
            return False, None
        ref = message.reference
        if ref is None or ref.message_id is None:
            return False, None
        entry = self._press_entries.get(ref.message_id)
        if entry is None:
            return False, None
        if time.monotonic() >= entry.expires:
            self._press_entries.pop(ref.message_id, None)
            return False, None
        if message.author.id != entry.requester_id and not is_owner(message.author.id):
            return True, None

        selector = message.content.strip()
        component = self._find_button_in(entry.buttons, selector)
        if component is None:
            if len(selector) <= 20:
                await message.reply(
                    f"no button '{selector}'. reply with a number.",
                    mention_author=False,
                )
            return True, None

        try:
            result = await press_and_collect(
                self.context.client, entry.target_for(component)
            )
        except Exception as e:
            await message.reply(
                f"the press failed 😟 details: {e}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True, None
        await self._apply_press_result(message, entry, result)
        return True, None

    async def _apply_press_result(
        self,
        origin: discord.Message,
        entry: PressEntry,
        result: Optional[HarvestedMessage],
    ) -> None:
        if result is None:
            await origin.reply(
                "i pressed it, but nothing visible happened 😟", mention_author=False
            )
            return

        content = self._echo_content(result)
        content, audio_files = await self._assemble_echo(result, entry.spec)
        content = content[:1990] or "pressed. but nothing selected to echo 😟"

        sent: Optional[discord.Message] = None
        if entry.echo is not None and not audio_files:
            try:
                await entry.echo.edit(
                    content=content, allowed_mentions=discord.AllowedMentions.none()
                )
                sent = entry.echo
            except discord.HTTPException:
                sent = None
        if sent is None:
            if audio_files:
                sent = await origin.reply(
                    content,
                    files=audio_files,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                sent = await origin.reply(
                    content,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        refreshed = self._register_pressable(
            sent, result, requester_id=entry.requester_id, spec=entry.spec
        )

        if refreshed is not None:
            self._press_entries[origin.id] = refreshed
        try:
            await origin.add_reaction("✅")
        except discord.HTTPException:
            pass

    def _echo_content(self, harvested: HarvestedMessage) -> str:
        text = harvested.render_compact(limit=1500)
        media = harvested.media_pairs() + [
            f"[{media_name(u, i)}]({u})" for i, u in enumerate(harvested.v2_media)
        ]
        if media:
            block = "\n".join(media)
            budget = max(0, 1990 - len(block) - 4)
            if text and len(text) > budget:
                text = text[:budget].rstrip() + "…"
            return f"{text}\n\n{block}" if text else block
        return text

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
                        description="the command type that will to be templated (leech, prefix, slash).",
                        usage_example="type=slash",
                        help_content="leech type represents my own commands, prefix for other bot's prefixes, and slash are for other bot's slash.",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="bot",
                        description="the target bot.",
                        usage_example="bot=@fergun",
                        help_content="the bot that this template targets, it is required to match leech's own id in self or native commands (keep as default).",
                        type=TypesRegistry.require(Types.USER),
                        required=False,
                    ),
                    Argument(
                        name="channel",
                        description="where to execute the invocation (defaults to here).",
                        usage_example="channel=<#749353351530938370>",
                        type=TypesRegistry.require(Types.CHANNEL),
                        required=False,
                    ),
                    Argument(
                        name="ai",
                        description="expose the template to the ai assistant (default: no).",
                        usage_example="ai=yes",
                        help_content=(
                            "when yes, the template shows up in my ai catalog "
                            "and the assistant may run it on your behalf. "
                            "subject to the usual per-user delegations "
                            "(>ai perms)."
                        ),
                        type=TypesRegistry.require(Types.BOOL),
                        required=False,
                    ),
                    Argument(
                        name="echo",
                        description="what and when to echo (kinds + auto/always/never).",
                        usage_example="echo=images",
                        help_content=(
                            "echo picks what gets presented: kinds are text, "
                            "image, video, audio, file, buttons (interaction), and media "
                            "(image+video+audio). a when word (auto, always, never) can join "
                            "the list. auto means echo only what the requester can't see "
                            "(remote or ephemeral). examples: `echo=images`, "
                            "`echo=always,media`, `echo=never`, `echo=text,audio,buttons`."
                        ),
                        type=TypesRegistry.require(Types.STR),
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
                subcommands={
                    "remove": Command(
                        name="remove",
                        description="remove a persisted template command.",
                        usage_example=">cmd invoke template remove name=img",
                        arguments=[
                            Argument(
                                name="name",
                                description="the template command name.",
                                usage_example="name=img",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                        subcommands={},
                        binding=NativeBinding(handler=self.template_remove_handler),
                    ),
                    "list": Command(
                        name="list",
                        description="list all template commands.",
                        usage_example=">cmd invoke template list",
                        arguments=[],
                        subcommands={},
                        binding=NativeBinding(handler=self.template_list_handler),
                    ),
                    "edit": Command(
                        name="edit",
                        description="edit a template's presentation without recreating it.",
                        usage_example=">cmd invoke template edit name=fc.level echo=always",
                        arguments=[
                            Argument(
                                name="name",
                                description="the template command name.",
                                usage_example="name=fc.level",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            ),
                            Argument(
                                name="echo",
                                description="the echo policy (auto, always, never).",
                                usage_example="echo=always",
                                type=TypesRegistry.require(Types.STR),
                                required=False,
                            ),
                            Argument(
                                name="channel",
                                description="pin to a channel.",
                                usage_example="channel=<#749353351530938370>",
                                type=TypesRegistry.require(Types.CHANNEL),
                                required=False,
                            ),
                            Argument(
                                name="unpin",
                                description="clear the pin (dynamic again).",
                                usage_example="unpin=yes",
                                type=TypesRegistry.require(Types.BOOL),
                                required=False,
                            ),
                            Argument(
                                name="ai",
                                description="show or hide the template from the ai assistant.",
                                usage_example="ai=no",
                                type=TypesRegistry.require(Types.BOOL),
                                required=False,
                            ),
                        ],
                        subcommands={},
                        binding=NativeBinding(handler=self.template_edit_handler),
                    ),
                },
                binding=NativeBinding(handler=self.invoke_template_handler),
                defaults={"channel": None, "echo": "auto"},
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
                        description="the command type that will to be invoked (leech, prefix, slash).",
                        usage_example="type=slash",
                        help_content="leech type represents my own commands, prefix for other bot's prefixes, and slash are for other bot's slash.",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    ),
                    Argument(
                        name="bot",
                        description="the target bot.",
                        usage_example="bot=@fergun",
                        help_content="the bot that this invocation targets, it is required to match leech's own id in self or native commands (keep as default).",
                        type=TypesRegistry.require(Types.USER),
                        required=False,
                    ),
                    Argument(
                        name="channel",
                        help_content="pin the execution channel. omit it and the "
                        "template runs wherever it is invoked (resolved at runtime, "
                        "not at registration).",
                        description="where to execute the invocation (defaults to here at runtime).",
                        usage_example="channel=<#749353351530938370>",
                        type=TypesRegistry.require(Types.CHANNEL),
                        required=False,
                    ),
                    Argument(
                        name="echo",
                        description="what and when to echo (kinds + auto/always/never).",
                        usage_example="echo=images",
                        help_content=(
                            "echo picks what gets presented: kinds are text, "
                            "image, video, audio, file, buttons (interaction), and media "
                            "(image+video+audio). a when-word (auto, always, never) can join "
                            "the list. auto means echo only what the requester can't see "
                            "(remote or ephemeral). examples: `echo=images`, "
                            "`echo=always,media`, `echo=never`, `echo=text,audio,buttons`."
                        ),
                        type=TypesRegistry.require(Types.STR),
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
                defaults={"channel": None, "echo": "auto"},
            )
        }

    def get_reap(self) -> dict[str, Command]:
        return {
            "reap": Command(
                name="reap",
                description="harvest a message and present it, normalized.",
                usage_example="(reply to a message) >cmd reap",
                help_content=(
                    "reply to any message (or pass message=<id>) and i'll harvest it. "
                    " (text, embeds, media, v2 components) and present it in text frinedly way. "
                    "echo selectors apply: `reap echo=images` shows only the images. "
                    "if the reaped message has buttons, my echo becomes pressable."
                ),
                arguments=[
                    Argument(
                        name="message",
                        description="the message id (default: replied to).",
                        usage_example="message=1560117814012940342",
                        type=TypesRegistry.require(Types.INT),
                        required=False,
                    ),
                    Argument(
                        name="channel",
                        description="where the message lives (for remote reaping).",
                        usage_example="channel=<#749353351530938370>",
                        type=TypesRegistry.require(Types.CHANNEL),
                        required=False,
                    ),
                    Argument(
                        name="echo",
                        description="what to present (kinds).",
                        usage_example="echo=images",
                        type=TypesRegistry.require(Types.STR),
                        required=False,
                    ),
                ],
                subcommands={},
                binding=NativeBinding(handler=self.reap_handler),
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

    async def invoke_handler(self, invocation: Invocation):
        invk_type = str(invocation.arguments["type"]).lower()
        raw_invocation = invocation.arguments["invocation"]
        bot = invocation.arguments.get("bot")
        match invk_type:
            case "slash":
                return await self.invoke_slash(invocation, bot, raw_invocation)
            case "leech" | "self" | "native":
                return await self.invoke_self(invocation, bot, raw_invocation)
            case "prefix":
                return await self.invoke_prefix(invocation, bot, raw_invocation)
            case _:
                raise CogError(
                    f"invalid command type: '{invk_type}' (try: leech, slash, prefix).",
                    is_user_error=True,
                )

    async def introspect_handler(self, invocation: Invocation):
        type = invocation.arguments["type"]
        bot = invocation.arguments.get("bot")
        match type:
            case "slash":
                introspection = await self.introspect_slash(invocation, bot)
            case "prefix":
                raise CogError(
                    f"it is impossible to introspect prefix commands.",
                    is_user_error=True,
                )
            case "leech" | "self" | "native":
                introspection = await self.introspect_leech(invocation, bot)
            case _:
                raise CogError(
                    f"invalid introspection type: '{type}'.", is_user_error=True
                )

        if not introspection:
            return

        msg = invocation.message
        for chunk in splitter.split(introspection):
            msg = await msg.reply(chunk)

    def _build_template_command(self, template: InvokeAlias) -> Command:
        prefix = {"slash": "/", "prefix": "!", "self": ">"}.get(template.type.value, "")
        return Command(
            name=template.name.split(".")[-1],
            description=f"**generated** template for {prefix}{' '.join(template.target)}",
            arguments=template.arguments,
            subcommands={},
            binding=NativeBinding(handler=self.invoke_template_trampoline_handler),
            defaults=template.defaults,
            ai_visible=template.ai,
        )

    async def _load_templates(self) -> None:
        TEMPLATES_STORE.parent.mkdir(parents=True, exist_ok=True)
        if not TEMPLATES_STORE.exists():
            return
        try:
            data = json.loads(TEMPLATES_STORE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _logger.error(f"failed to load templates: {e}")
            return

        for entry in data.get("templates", []):
            try:
                template = template_from_dict(entry)
            except Exception as e:
                _logger.warning(f"skipping malformed template entry: {e}")
                continue

            if template.name in self.invoke_aliases:
                _logger.warning(f"skipping duplicate template '{template.name}'.")
                continue

            self.invoke_aliases[template.name] = template
            self._refresh_groups()

            try:
                self._register_template(template)
            except CogError as e:
                self.invoke_aliases.pop(template.name, None)
                self._refresh_groups()
                _logger.error(f"skipping template '{template.name}': {e.message}")
            self._refresh_groups()
        self._refresh_groups()

    def _save_templates(self) -> None:
        tmp = TEMPLATES_STORE.with_suffix(".json.temp")
        tmp.write_text(
            json.dumps(
                {
                    "templates": [
                        template_to_dict(t) for t in self.invoke_aliases.values()
                    ]
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, TEMPLATES_STORE)

    def _refresh_groups(self) -> None:
        self._template_groups = {
            name.split(".")[0] for name in self.invoke_aliases if "." in name
        }

    def _register_template(self, template: InvokeAlias) -> None:
        segments = template.name.split(".")
        command = self._build_template_command(template)
        registry = self.context.client.commands

        if len(segments) == 1:
            name = segments[0]
            old = registry.get_command(name)
            if old is not None:
                if name not in self.invoke_aliases:
                    raise CogError(
                        f"'{name}' is already registered by a non-template "
                        "command. pick another name 😁.",
                        is_user_error=True,
                    )
                command.subcommands = old.subcommands   # keep nested templates alive
                registry.unregister_command(name)
            if not self.context.add_command(name, command):
                raise CogError(f"failed to register '{name}'.", is_user_error=True)
            return

        root_name = segments[0]
        root = registry.get_command(root_name)
        if root is None:
            root = self._stub(root_name, is_root=True)
        elif (
            root_name not in self.invoke_aliases
            and root_name not in self._template_groups
        ):
            raise CogError(
                f"'{root_name}' is a built-in command. i can't nest under it. "
                "pick another root.",
                is_user_error=True,
            )

        container = root
        for segment in segments[1:-1]:
            child = container.subcommands.get(segment)
            if child is None:
                child = self._stub(segment)
                container.subcommands[segment] = child
            container = child

        leaf = segments[-1]
        old = container.subcommands.get(leaf)
        if old is not None:
            # our own leaf (edit / overwrite / stub from a deeper template):
            # replace in place, keep deeper templates nested under it alive.
            command.subcommands = old.subcommands
        container.subcommands[leaf] = command

        registry.unregister_command(root_name)
        if not self.context.add_command(root_name, root):
            raise CogError(
                f"failed to refresh group '{root_name}'.",
                is_user_error=True,
            )

    # def _register_template(self, template: InvokeAlias) -> None:
    #     segments = template.name.split(".")
    #     command = self._build_template_command(template)
    #     registry = self.context.client.commands
    #
    #     if len(segments) == 1:
    #         name = segments[0]
    #
    #         if registry.get_command(name) is not None:
    #             registry.unregister_command(name)
    #
    #         if not self.context.add_command(name, command):
    #             raise CogError(
    #                 f"'{name}' is already registered by a non-template "
    #                 "command. pick another name 😁.",
    #                 is_user_error=True,
    #             )
    #         return
    #
    #     root_name = segments[0]
    #     root = registry.get_command(root_name)
    #
    #     if root is None:
    #         root = self._stub(root_name, is_root=True)
    #
    #     elif (
    #         root_name not in self.invoke_aliases
    #         and root_name not in self._template_groups
    #     ):
    #         raise CogError(
    #             f"'{root_name}' is a built-in command. i can't nest under it. "
    #             "pick another root.",
    #             is_user_error=True,
    #         )
    #
    #     container = root
    #     for segment in segments[1:-1]:
    #         child = container.subcommands.get(segment)
    #
    #         if child is None:
    #             child = self._stub(segment)
    #             container.subcommands[segment] = child
    #
    #         container = child
    #
    #     leaf = segments[-1]
    #
    #     if leaf in container.subcommands:
    #         raise CogError(
    #             f"'{template.name}' is already in use.",
    #             is_user_error=True,
    #         )
    #
    #     container.subcommands[leaf] = command
    #
    #     _logger.debug(
    #         "REGISTER TREE %s: %r",
    #         root_name,
    #         root.subcommands,
    #     )
    #
    #     registry.unregister_command(root_name)
    #     if not self.context.add_command(root_name, root):
    #         raise CogError(
    #             f"failed to refresh group '{root_name}'.",
    #             is_user_error=True,
    #         )
    #
    def _stub(self, name: str, *, is_root: bool = False) -> Command:
        return Command(
            name=name,
            description="**generated** template group.",
            arguments=[],
            subcommands={},
            binding=MockBinding(),
            is_root=is_root,
        )

    def _detach_template(self, dotted: str) -> None:
        segments = dotted.split(".")
        self.context.client.commands.unregister_command(dotted)  # flat path entry
        if len(segments) == 1:
            return

        root_name = segments[0]
        root = self.context.client.commands.get_command(root_name)
        if root is None:
            return
        node = root
        for segment in segments[1:-1]:
            node = node.subcommands.get(segment)
            if node is None:
                return
        node.subcommands.pop(segments[-1], None)
        if (
            not root.subcommands
            and root_name in self._template_groups
            and isinstance(root.binding, MockBinding)
        ):
            self.context.client.commands.unregister_command(root_name)
        else:
            self.context.client.commands.unregister_command(root_name)
            self.context.add_command(root_name, root)

    async def invoke_template_handler(self, invocation: Invocation):
        name = invocation.arguments["command"]
        if not all(is_valid_identifier(s) for s in name.split(".")):
            raise CogError(
                f"'{name}' is not a valid command name: each dot separated segment "
                "must be a single word (letters, digits, `_`, `-`). dots nest: "
                "`fc.level` registers under the `fc` group.",
                is_user_error=True,
            )

        raw_type = str(invocation.arguments["type"]).lower()
        bot = invocation.arguments.get("bot")
        raw_invocation = invocation.arguments["invocation"]
        channel = invocation.arguments.get("channel")
        echo = invocation.arguments.get("echo")
        echo = (
            None if echo is None or str(echo).lower() == "auto" else str(echo).lower()
        )
        ai = bool(invocation.arguments.get("ai"))

        template_type: ProviderType
        arguments: list[Argument]
        defaults: dict[str, Any]
        target: list[str]

        match raw_type:
            case "slash":
                template_type = ProviderType.SLASH
                if bot is None:
                    raise CogError(
                        "'bot' argument is required for slash templates.",
                        is_user_error=True,
                    )
                site = await self._channel_of(channel, invocation)
                result = await self.resolve_slash_invocation(
                    channel=site,
                    bot=bot,
                    raw_invocation=raw_invocation,
                    check_missing=False,
                )
                target, arguments, defaults = (
                    result.resolved.target,
                    result.arguments,
                    result.parsed,
                )

            case "prefix":
                template_type = ProviderType.PREFIX
                text = raw_invocation.strip()
                if not text:
                    raise CogError(
                        "'invocation' can't be empty for prefix templates.",
                        is_user_error=True,
                    )
                target = [text]
                arguments = [
                    Argument(
                        name="args",
                        description="runtime arguments appended to the stored prefix command.",
                        usage_example="args=volume 50",
                        type=TypesRegistry.require(Types.STR),
                        required=False,
                    )
                ]
                defaults = {}

            case "leech" | "self" | "native":
                template_type = ProviderType.SELF
                if echo is not None or channel is not None:
                    raise CogError(
                        "leech templates don't take echo or pinned channels. the "
                        "chained command presents itself wherever invoked.",
                        is_user_error=True,
                    )
                if (
                    bot is not None and bot != self.context.client.user.id
                ):  # pyright: ignore
                    raise CogError(
                        "'bot' doesn't apply to leech templates - leave it blank.",
                        is_user_error=True,
                    )
                lexed = lexer.lex(raw_invocation, prefixes=[])
                if not lexed.tokens:
                    raise CogError("'invocation' can't be empty.", is_user_error=True)
                if lexed.tokens[0].type != TokenType.RAW or not is_valid_identifier(
                    lexed.tokens[0].raw
                ):
                    raise CogError(
                        f"'{lexed.tokens[0].raw}' is not a valid command identifier.",
                        is_user_error=True,
                    )
                resolved = command_resolver.resolve_command(
                    lexed.tokens, self.context.client.commands
                )
                if resolved is None:
                    raise CogError(
                        "you have provided an invalid command\nmaybe try ``>help``?",
                        is_user_error=True,
                    )
                if not is_invocable(resolved.command.binding):
                    raise CogError(
                        "can't template a group. target one of its subcommands.",
                        is_user_error=True,
                    )
                target = resolved.path.split(".")
                arguments = resolved.command.arguments
                defaults = parser.parse_args(
                    parser.ParseContext(
                        arguments=resolved.command.arguments, tokens=resolved.args
                    ),
                    check_missing=False,
                )

            case _:
                raise CogError(
                    f"invalid template type: '{raw_type}' (try: slash, prefix, leech).",
                    is_user_error=True,
                )

        template = InvokeAlias(
            name=name,
            type=template_type,
            bot=bot,
            target=target,
            arguments=arguments,
            defaults=defaults,
            channel=channel,
            echo=echo,
            ai=ai,
        )

        self.invoke_aliases[template.name] = template
        self._register_template(template)
        self._refresh_groups()
        self._save_templates()
        await invocation.message.reply(
            f"i added command: `{template.name}`, with template: "
            f"```yaml\n{format_template(template)}\n```"
        )

    async def invoke_template_trampoline_handler(self, invocation: Invocation):
        path = (
            self.context.client.commands.path_of(invocation.command)
            or invocation.command.name
        )
        template = self.invoke_aliases.get(path)
        if template is None:
            raise CogError(f"template '{path}' doesn't exist 😟", is_user_error=True)
        match template.type:
            case ProviderType.SLASH:
                site = await self._channel_of(template.channel, invocation)
                resolved = await resolver.resolve_target(
                    site, template.target, template.bot
                )  # pyright: ignore
                arguments = await self._fill_attachments(
                    invocation, resolved, invocation.arguments
                )
                execution = await execute_slash(
                    self.context.client,
                    resolved,
                    site,
                    arguments,
                    application_id=template.bot,  # pyright: ignore
                    command_name=" ".join(template.target),
                )
                invocation.chain.executions.append(execution)
                await self._present(
                    invocation, execution, site=site, echo=template.echo
                )

            case ProviderType.PREFIX:
                site = await self._channel_of(template.channel, invocation)
                text = " ".join(template.target)
                extra = invocation.arguments.get("args")
                if extra:
                    text = f"{text} {extra}"
                files = await self._invocation_files(invocation)
                text, reference = await self._forward_reference(invocation, text, site)
                if template.bot is None:
                    await site.send(
                        text, files=files or None, reference=reference
                    )  # pyright: ignore
                    return None
                execution = await execute_prefix(
                    self.context.client,
                    site,
                    text,
                    application_id=template.bot,
                    files=files,
                    reference=reference,
                )
                invocation.chain.executions.append(execution)
                await self._present(
                    invocation, execution, site=site, echo=template.echo
                )

            case ProviderType.SELF:
                if template.channel is not None:
                    raise CogError(
                        "leech templates run wherever invoked (no pinned channel).",
                        is_user_error=True,
                    )
                tokens = [Token(TokenType.RAW, part) for part in template.target]
                resolved = command_resolver.resolve_command(
                    tokens, self.context.client.commands
                )
                if resolved is None:
                    raise CogError(
                        f"template target no longer exists: '{'.'.join(template.target)}'.",
                        is_user_error=True,
                    )
                if resolved.args:
                    raise CogError(
                        "template target resolved ambiguously.", is_user_error=True
                    )
                return invocation.derive(resolved.command, invocation.arguments)

            case _:
                raise CogError("unsupported template type at the moment.")

    async def template_remove_handler(self, invocation: Invocation):
        name = str(invocation.arguments["name"]).strip()
        if name not in self.invoke_aliases:
            raise CogError(f"no template command: '{name}'.", is_user_error=True)
        self._detach_template(name)
        self.invoke_aliases.pop(name)
        self._refresh_groups()
        self._save_templates()
        await invocation.message.reply(f"removed template: `{name}`.")

    async def template_list_handler(self, invocation: Invocation):
        if not self.invoke_aliases:
            await invocation.message.reply("no template commands.")
            return
        prefix = {"slash": "/", "prefix": "", "self": ">"}
        lines = [
            f"`{t.name}` → {prefix.get(t.type.value, '')}{' '.join(t.target)}"
            + (f" on <@{t.bot}>" if t.bot is not None else "")
            + (f" pinned to <#{t.channel}>" if t.channel is not None else " (dynamic)")
            + (f" (echo: {t.echo})" if t.echo else "")
            + (" [ai]" if t.ai else "")
            for t in sorted(self.invoke_aliases.values(), key=lambda t: t.name)
        ]
        await invocation.message.reply("\n".join(lines))

    async def template_edit_handler(self, invocation: Invocation):
        name = str(invocation.arguments["name"]).strip()
        template = self.invoke_aliases.get(name)
        if template is None:
            raise CogError(f"no template command: '{name}'.", is_user_error=True)

        echo = invocation.arguments.get("echo")
        channel = invocation.arguments.get("channel")
        unpin = invocation.arguments.get("unpin")
        ai = invocation.arguments.get("ai")

        if template.type is ProviderType.SELF and (
            echo is not None or channel is not None
        ):
            raise CogError(
                "leech templates don't take echo or pinned channels.",
                is_user_error=True,
            )
        if echo is not None:
            try:
                EchoSpec.parse(echo)
            except ValueError as e:
                raise CogError(str(e), is_user_error=True) from e
            template.echo = None if str(echo).lower() == "auto" else str(echo).lower()
        if channel is not None:
            template.channel = channel
        if unpin:
            template.channel = None
        if ai is not None:
            template.ai = True

        self._register_template(template)
        self._save_templates()
        await invocation.message.reply(
            f"edited `{name}`: ```yaml\n{format_template(template)}\n```"
        )

    async def resolve_slash_invocation(
        self,
        channel,
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
            channel,
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

    async def _channel_of(self, channel_id: int | None, invocation: Invocation):
        if channel_id is None:
            return invocation.message.channel
        channel = self.context.client.get_channel(channel_id)
        if channel is None:
            channel = await self.context.client.fetch_channel(channel_id)
        return channel

    async def _site_channel(self, invocation: Invocation):
        return await self._channel_of(invocation.arguments.get("channel"), invocation)

    async def _media_files(
        self, harvested, *, force: bool = False
    ) -> list[discord.File]:
        media = harvested.file_media()
        if force:
            media = media + [
                (url, url.split("/")[-1].split("?")[0] or "image")
                for url in harvested.image_media()[:ECHO_MEDIA_CAP]
            ]
        media = media[:ECHO_MEDIA_CAP]
        if not media:
            return []

        async def fetch(client: httpx.AsyncClient, url: str, filename: str):
            try:
                response = await client.get(url)
                response.raise_for_status()
                if len(response.content) > ECHO_FILE_MAX_BYTES:
                    return None
                return discord.File(io.BytesIO(response.content), filename=filename)
            except httpx.HTTPError:
                return None

        async with httpx.AsyncClient(timeout=15.0) as client:
            results = await asyncio.gather(
                *(fetch(client, url, name) for url, name in media)
            )
        return [file for file in results if file is not None]

    async def _assemble_echo(
        self, harvested: HarvestedMessage, spec: EchoSpec
    ) -> tuple[str, list[discord.File]]:
        buttons = [
            c for c in harvested.components if c.kind == "button" and not c.disabled
        ]

        text = (
            harvested.render_compact(limit=1500, include_components=False)
            if spec.wants("text")
            else ""
        )

        links: list[str] = []
        if spec.wants("image"):
            urls = harvested.image_urls()[:ECHO_MEDIA_CAP]
            links += [f"[{media_name(u, i)}]({u})" for i, u in enumerate(urls)]
        if spec.wants("video"):
            links += [a.url for a in harvested.attachments_of({"video"})]
        if spec.wants("file"):
            links += [a.url for a in harvested.attachments_of({"file"})]

        audio_files: list[discord.File] = []
        if spec.wants("audio"):
            audio_files = await self._upload(
                harvested.attachments_of({"audio"})[:ECHO_MEDIA_CAP]
            )

        content = text
        if spec.wants("buttons") and buttons:
            line = _press_line(buttons)
            content = f"{content}\n{line}" if content else line
        if links:
            block = "\n".join(links)
            budget = max(0, 1990 - len(block) - 4)
            if content and len(content) > budget:
                content = content[:budget].rstrip() + "…"
            content = f"{content}\n\n{block}" if content else block
        if audio_files and not content:
            content = f"(received {len(audio_files)} file(s))"
        return content, audio_files

    async def _upload(
        self, attachments: list[HarvestedAttachment]
    ) -> list[discord.File]:
        async def fetch(client: httpx.AsyncClient, att: HarvestedAttachment):
            try:
                response = await client.get(att.url)
                response.raise_for_status()
                if len(response.content) > ECHO_FILE_MAX_BYTES:
                    return None
                return discord.File(
                    io.BytesIO(response.content), filename=att.filename or "audio"
                )
            except httpx.HTTPError:
                return None

        async with httpx.AsyncClient(timeout=15.0) as client:
            results = await asyncio.gather(*(fetch(client, a) for a in attachments))
        return [f for f in results if f is not None]

    async def _present(
        self, invocation: Invocation, execution, *, site, echo: str | None = None
    ) -> None:
        try:
            spec = EchoSpec.parse(echo)
        except ValueError as e:
            raise CogError(str(e), is_user_error=True) from e
        if spec.when == "never":
            return
        harvested = await execution.settle()
        if harvested is None:
            await invocation.message.reply("the remote side never answered 😟")
            return

        should = (
            spec.when == "always"
            or site.id != invocation.message.channel.id
            or harvested.is_ephemeral
        )
        if not should:
            if invocation.via == "ai":
                return
            buttons = [
                c for c in harvested.components if c.kind == "button" and not c.disabled
            ]
            if buttons:
                sent = await invocation.message.reply(
                    _press_line(buttons),
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                self._register_pressable(
                    sent,
                    harvested,
                    requester_id=invocation.message.author.id,
                    spec=spec,
                )
            return

        content, audio_files = await self._assemble_echo(harvested, spec)
        if not content and not audio_files:
            content = "i received a response, but you asked me to echo nothing 😁"

        if audio_files:
            sent = await invocation.message.reply(
                content,
                files=audio_files,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            sent = await invocation.message.reply(
                content,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        self._register_pressable(
            sent, harvested, requester_id=invocation.message.author.id, spec=spec
        )

    async def _forward_reference(self, invocation: Invocation, text: str, site):
        ref = invocation.message.reference
        if ref is None or ref.message_id is None:
            return text, None

        if site.id == invocation.message.channel.id:
            return text, ref

        resolved = ref.cached_message or getattr(ref, "resolved", None)
        if resolved is None:
            try:
                resolved = await invocation.message.channel.fetch_message(
                    ref.message_id
                )
            except discord.HTTPException:
                resolved = None

        if isinstance(resolved, discord.Message) and resolved.content.strip():
            return f"{text}\n{resolved.content.strip()}", None
        return text, None

    async def _fill_attachments(
        self, invocation: Invocation, resolved, arguments: dict
    ) -> dict:
        sources = await gather_attachments(invocation.message)
        for option in resolved.command.options:
            if option.type != discord.ApplicationCommandOptionType.attachment:
                continue
            value = arguments.get(option.name)
            if value is None:
                if sources:
                    arguments[option.name] = await sources.pop(0).to_file()
                continue
            arguments[option.name] = await self._url_to_file(value)
        return arguments

    async def _invocation_files(self, invocation: Invocation) -> list[discord.File]:
        files: list[discord.File] = []
        for att in await gather_attachments(invocation.message):
            try:
                files.append(await att.to_file())
            except discord.HTTPException:
                continue
        return files[:ECHO_MEDIA_CAP]

    async def _url_to_file(self, value) -> Optional[discord.File]:
        url = getattr(value, "url", value)
        filename = getattr(value, "filename", None) or (
            url.split("/")[-1].split("?")[0] or "file"
        )
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                if len(response.content) > ECHO_FILE_MAX_BYTES:
                    return None
                return discord.File(io.BytesIO(response.content), filename=filename)
        except httpx.HTTPError:
            return None

    async def invoke_slash(
        self, invocation: Invocation, bot: int | None, raw_invocation: str
    ):
        if bot is None:
            raise CogError(
                "'bot' argument is required for slash invocation.", is_user_error=True
            )
        site = await self._site_channel(invocation)
        result = await self.resolve_slash_invocation(
            channel=site, bot=bot, raw_invocation=raw_invocation
        )
        arguments = await self._fill_attachments(
            invocation, result.resolved, result.parsed
        )
        execution = await execute_slash(
            self.context.client,
            result.resolved,
            site,
            arguments,
            application_id=bot,
            command_name=" ".join(result.resolved.target),
        )
        invocation.chain.executions.append(execution)
        await self._present(
            invocation,
            execution,
            site=site,
            echo=str(invocation.arguments.get("echo") or "auto"),
        )
        return None

    async def invoke_prefix(
        self, invocation: Invocation, bot: int | None, raw_invocation: str
    ):
        text = raw_invocation.strip()
        if len(text) > splitter.DISCORD_MESSAGE_LIMIT:
            raise CogError(
                f"prefix command too long. (limit is {splitter.DISCORD_MESSAGE_LIMIT})."
            )
        site = await self._site_channel(invocation)
        files = await self._invocation_files(invocation)
        text, reference = await self._forward_reference(invocation, text, site)
        if bot is None:
            await site.send(
                text, files=files or None, reference=reference
            )  # pyright: ignore
            return None
        execution = await execute_prefix(
            self.context.client,
            site,
            text,
            application_id=bot,
            files=files,
            reference=reference,
        )
        invocation.chain.executions.append(execution)
        await self._present(
            invocation,
            execution,
            site=site,
            echo=str(invocation.arguments.get("echo") or "auto"),
        )
        return None

    async def invoke_self(
        self, invocation: Invocation, bot: int | None, raw_invocation: str
    ):
        if bot is not None and bot != self.context.client.user.id:  # pyright: ignore
            raise CogError(
                "'bot' doesn't apply to leech commands. leave it blank (or pass my "
                "own id). to run another bot's command use type=slash or type=prefix.",
                is_user_error=True,
            )
        lexed = lexer.lex(raw_invocation, prefixes=[])
        if not lexed.tokens:
            raise CogError(
                "'invocation' is empty, i can't do anything with it.",
                is_user_error=True,
            )
        if lexed.tokens[0].type != TokenType.RAW or not is_valid_identifier(
            lexed.tokens[0].raw
        ):
            raise CogError(
                f"you have provided an invalid command identifier: '{lexed.tokens[0].raw}'.",
                is_user_error=True,
            )
        resolved = command_resolver.resolve_command(
            lexed.tokens, self.context.client.commands
        )
        if resolved is None:
            raise CogError(
                "you have provided an invalid command\nmaybe try ``>help``?",
                is_user_error=True,
            )
        arguments = parser.parse_args(
            parser.ParseContext(
                arguments=resolved.command.arguments, tokens=resolved.args
            )
        )
        return invocation.derive(resolved.command, arguments)

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

    async def introspect_leech(self, invocation: Invocation, bot: int | None):
        if bot is not None and bot != self.context.client.user.id:  # pyright: ignore
            raise CogError(
                "'bot' doesn't apply to leech commands (or better leave it blank 😁).",
                is_user_error=True,
            )
        lines: list[str] = []
        for path, command in self.context.client.commands.registered_commands.items():
            if not command.is_root or not is_invocable(command.binding):
                continue
            arguments = ", ".join(a.name for a in command.arguments)
            suffix = f" ({arguments})" if arguments else ""
            lines.append(f"{command.name}{suffix} - {command.description}")
        return "\n".join(lines) or "no leech commands registered."

    async def reap_handler(self, invocation: Invocation):
        message_id = invocation.arguments.get("message")
        if message_id is None:
            ref = invocation.message.reference
            if ref is None or ref.message_id is None:
                raise CogError(
                    "reply to the message you want reaped, or pass message=<id>.",
                    is_user_error=True,
                )
            message_id = ref.message_id

        site = await self._channel_of(invocation.arguments.get("channel"), invocation)
        try:
            msg = await site.fetch_message(message_id)  # pyright: ignore
        except discord.HTTPException as e:
            raise CogError(f"couldn't fetch that message: {e}", is_user_error=True)

        try:
            spec = EchoSpec.parse(invocation.arguments.get("echo"))
        except ValueError as e:
            raise CogError(str(e), is_user_error=True) from e
        if spec.when == "never":
            raise CogError(
                "reaping with echo=never would show nothing 😁", is_user_error=True
            )
        spec = EchoSpec(when="always", kinds=spec.kinds)

        harvested = await harvest_full(msg, self.context.client)
        content, audio_files = await self._assemble_echo(harvested, spec)
        if not content and not audio_files:
            content = "i reaped it, but there was nothing to show 😟"

        if audio_files:
            sent = await invocation.message.reply(
                content,
                files=audio_files,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            sent = await invocation.message.reply(
                content,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        self._register_pressable(
            sent, harvested, requester_id=invocation.message.author.id, spec=spec
        )
        return None


def create_cog(context: CogContext):
    return CommandCog(context=context)
