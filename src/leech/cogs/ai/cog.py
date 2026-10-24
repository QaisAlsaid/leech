from __future__ import annotations

import discord
from pydantic import ValidationError

from leech.core.cog.cog import Cog
from leech.core.cog.context import CogContext, Priority
from leech.core.cog.error import CogError
from leech.core.command.chain import Chain
from leech.core.command.invocation import Invocation
from leech.core.command.mock.binding import MockBinding
from leech.core.command.native.binding import NativeBinding
from leech.core.command.runtime import Argument, Command
from leech.core.command.slash.binding import SlashBinding
from leech.core.command.helpers import is_invocable
from leech.core.permissions import is_owner
from leech.core.ai import schema
from leech.core.ai.audit import ActionLog
from leech.core.ai.service import AiError, AiService, Attachment
from leech.core.helpers.interactions import harvest
from leech.core.helpers.interactions.reply import ReplySession
from leech.core.helpers.attachments.attachments import gather_attachments
import leech.config as config
from leech.core.error import BaseError
from leech.core.parser.types.registry import Types, TypesRegistry
import leech.core.helpers.text.paginator as paginator

import asyncio
import json
import logging
import os
import re

_logger = logging.getLogger(__name__)

REPLY_LIMIT = 1990
ATTEMPTS = 3
MAX_ROUNDS = 3
MODEL_CALL_BUDGET = 4

# v10.9 AI_OPSEC_3: how long the ai waits on ONE command before letting
# it run on in the background (ytdlp/tts take minutes; blocking the turn
# that long starved the read path and bred retries + double replies).
AI_CMD_TIMEOUT = float(os.getenv("AI_COMMAND_TIMEOUT", "45") or 45)
# v10.9 AI_OPSEC_4: how long the user has to answer an ask-mode
# confirmation before it counts as a decline.
AI_CONFIRM_TIMEOUT = float(os.getenv("AI_CONFIRM_TIMEOUT", "120") or 120)
CONFIRM_RE = re.compile(
    r"^\s*(?:yes|y|allow|ok|always|no|n|deny|stop|never)\b", re.IGNORECASE
)
CONFIRM_YES = {"yes", "y", "allow", "ok", "sure", "go"}
CONFIRM_ALWAYS = {"always", "forever"}
CONFIRM_NO = {"no", "n", "deny", "stop", "cancel"}
CONFIRM_NEVER = {"never"}
MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

import httpx
from leech.core.helpers.interactions.model import media_kind

URL_RE = re.compile(r"https?://[^\s)\"'<>\]]+")

AI_URL = (
    (os.getenv("LEECH_AI_URL") or os.getenv("LEECH_TTS_URL") or "").strip().rstrip("/")
)
AI_TOKEN = (os.getenv("LEECH_AI_TOKEN") or os.getenv("LEECH_TTS_TOKEN") or "").strip()
AI_BACKEND = (os.getenv("LEECH_AI_BACKEND") or "").strip().lower()
AI_KEYS = bool(
    (os.getenv("GEMINI_API_KEY") or "").strip()
    or (os.getenv("GROQ_API_KEY") or "").strip()
)


class AiCog(Cog):
    def note(self, channel_id: int, text: str) -> None:
        if channel_id in self.ai.active:
            self.ai.window(channel_id).push("(system)", 0, text)

    def __init__(self, context: CogContext):
        super().__init__(context=context)
        if not (AI_URL or AI_KEYS or AI_BACKEND):
            _logger.warning(
                "ai cog: no manager url (LEECH_AI_URL or LEECH_TTS_URL) and no "
                "provider keys (GEMINI_API_KEY / GROQ_API_KEY). not loading"
            )
            return
        self.context.add_on_load(self.load)
        self.context.add_on_message(Priority.LOW, self.on_message)
        self.ai = AiService(
            manager_url=AI_URL,
            manager_token=AI_TOKEN,
            thinking=config.ai_thinking,
            turn_deadline=config.ai_turn_deadline,
            window_limit=config.ai_window_limit,
            window_budget=config.ai_window_budget,
            backend="direct",
        )
        self.context.add_on_unload(self.shutdown)
        self.log = ActionLog()

    async def shutdown(self) -> None:
        await self.ai.aclose()

    async def load(self):
        ai = Command(
            name="ai",
            description="chat with the leech, it can also run commands for you.",
            usage_example=">ai talk add buy milk to my todo",
            help_content=(
                "i answer mentions and replies, or `>ai talk`.\n"
                "when a request maps to a command i will try to run it on your behalf."
            ),
            arguments=[],
            subcommands=(
                self.get_talk()
                | self.get_perms()
                | self.get_dry()
                | self.get_persona()
                | self.get_model()
                | self.get_catalog()
                | self.get_clear()
                | self.get_on()
                | self.get_off()
                | self.get_status()
            ),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(ai.name, ai):
            raise CogError(f"i failed to load '{ai.name}' command.")

    def get_model(self) -> dict[str, Command]:
        def leaf(
            name: str, description: str, handler, arguments=None, elevated: bool = False
        ) -> Command:
            return Command(
                name=name,
                description=description,
                arguments=arguments or [],
                subcommands={},
                binding=NativeBinding(handler=handler),
                elevated=elevated,
            )

        return {
            "model": Command(
                name="model",
                description="view and reconfigure my model chain (owner only).",
                usage_example=">ai model list",
                help_content=(
                    "the chain is ordered: the first model is the main, the rest are fallbacks, they run in order."
                ),
                arguments=[],
                binding=MockBinding(),
                subcommands={
                    "list": leaf(
                        "list", "show the model chain.", self.model_list_handler
                    ),
                    "add": leaf(
                        "add",
                        "add a model to the chain.",
                        self.model_add_handler,
                        [
                            Argument(
                                name="model",
                                description="provider:model, e.g. groq:openai/gpt-oss-120b.",
                                usage_example="model=groq:openai/gpt-oss-20b",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            ),
                            Argument(
                                name="position",
                                description="insert position (0 = main).",
                                usage_example="position=0",
                                type=TypesRegistry.require(Types.INT),
                                required=False,
                            ),
                        ],
                        elevated=True,
                    ),
                    "remove": leaf(
                        "remove",
                        "remove a model from the chain.",
                        self.model_remove_handler,
                        [
                            Argument(
                                name="model",
                                description="the provider:model to remove.",
                                usage_example="model=gemini:gemini-3.5-flash-lite",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                        elevated=True,
                    ),
                    "main": leaf(
                        "main",
                        "promote a model to the top of the chain.",
                        self.model_main_handler,
                        [
                            Argument(
                                name="model",
                                description="the provider:model to promote.",
                                usage_example="model=gemini:gemini-3.5-flash-lite",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                        elevated=True,
                    ),
                    "reset": leaf(
                        "reset",
                        "restore the startup chain.",
                        self.model_reset_handler,
                        elevated=True,
                    ),
                    "budget": leaf(
                        "budget",
                        "set the context window size (chars).",
                        self.model_budget_handler,
                        [
                            Argument(
                                name="chars",
                                description="total rendered window budget in characters (~4 chars/token).",
                                usage_example="chars=8000",
                                type=TypesRegistry.require(Types.INT),
                                required=True,
                            )
                        ],
                    ),
                },
            )
        }

    def get_perms(self) -> dict[str, Command]:
        def leaf(name, description, handler, arguments=None):
            return Command(
                name=name,
                description=description,
                arguments=arguments or [],
                subcommands={},
                binding=NativeBinding(handler=handler),
            )

        command_arg = Argument(
            name="command",
            description="the command path (or pattern like todo.*) to configure.",
            usage_example="command=todo.discard",
            type=TypesRegistry.require(Types.STR),
            required=True,
        )
        user_arg = Argument(
            name="user",
            description="whose delegation to edit (owner only, defaults to your id).",
            usage_example="user=@opposingfork",
            type=TypesRegistry.require(Types.USER),
            required=False,
        )
        return {
            "perms": Command(
                name="perms",
                description="configure what i may run for you (allow / deny / ask).",
                usage_example=">ai perms deny command=todo.discard",
                help_content=(
                    "v10.9: delegation is now three states. `allow` runs "
                    "without asking, `deny` never runs, and `ask` (the "
                    "default for everything) confirms with you in chat "
                    "before running - reply yes / always / no / never to "
                    "those prompts. `always` and `never` are remembered "
                    "here."
                ),
                arguments=[],
                binding=MockBinding(),
                subcommands={
                    "list": leaf(
                        "list",
                        "show your delegation rules.",
                        self.perms_list_handler,
                        [user_arg],
                    ),
                    "deny": leaf(
                        "deny",
                        "deny the assistant a command for you.",
                        self.perms_deny_handler,
                        [command_arg, user_arg],
                    ),
                    "allow": leaf(
                        "allow",
                        "let the assistant run a command without asking.",
                        self.perms_allow_handler,
                        [command_arg, user_arg],
                    ),
                    "ask": leaf(
                        "ask",
                        "confirm with me before running this command (the default).",
                        self.perms_ask_handler,
                        [command_arg, user_arg],
                    ),
                },
            )
        }

    def _delegation_target(self, invocation: Invocation) -> int:
        user = invocation.arguments.get("user")
        if user is not None and user != invocation.message.author.id:
            if not is_owner(invocation.message.author.id):
                raise CogError(
                    "only owner may configure someone else's delegation.",
                    is_user_error=True,
                )
            return user
        return invocation.message.author.id

    async def perms_list_handler(self, invocation: Invocation):
        target = self._delegation_target(invocation)
        entries = self.context.client.security.delegations_of(target)
        lines = [
            f"`{scope}`: {'allowed' if allow else 'denied'}"
            for scope, allow in sorted(entries.items())
        ]
        tail = (
            "everything else: `ask` - i confirm with you in chat before "
            "running it (yes / always / no / never)."
        )
        if lines:
            await invocation.message.reply(
                "\n".join(lines) + f"\n{tail}", mention_author=False
            )
        else:
            await invocation.message.reply(
                f"no explicit rules yet. {tail}", mention_author=False
            )

    async def perms_ask_handler(self, invocation: Invocation):
        target = self._delegation_target(invocation)
        path = invocation.arguments["command"].strip()
        self.context.client.security.ask_delegation(target, f"command:{path}")
        await invocation.message.reply(
            f"i'll ask <@{target}> before running `{path}` from now on.",
            mention_author=False,
        )

    async def perms_deny_handler(self, invocation: Invocation):
        target = self._delegation_target(invocation)
        path = invocation.arguments["command"].strip()
        self.context.client.security.deny_delegation(target, f"command:{path}")
        await invocation.message.reply(
            f"the assistant may no longer run `{path}` for <@{target}>."
        )

    async def perms_allow_handler(self, invocation: Invocation):
        target = self._delegation_target(invocation)
        path = invocation.arguments["command"].strip()
        self.context.client.security.allow_delegation(target, f"command:{path}")
        await invocation.message.reply(
            f"the assistant may run `{path}` for <@{target}> without asking."
        )

    def _require_owner(self, invocation: Invocation) -> None:
        if not is_owner(invocation.message.author.id):
            raise CogError("only owner touches my private parts 😁", is_user_error=True)

    async def _chain_reply(self, prefix: str) -> str:
        try:
            lines, _daily = await self.ai.usage()
        except AiError as e:
            return f"{prefix}\n  (couldn't list the chain: {e.message})"
        body = "\n".join(f"  {line}" for line in lines)
        return f"{prefix}\n{body}"

    def get_talk(self) -> dict[str, Command]:
        return {
            "talk": Command(
                name="talk",
                description="talk to me directly.",
                usage_example=">ai talk what on my todo list?",
                help_content="same as mentioning me, but explicit. i may answer in chat or run a command for you.",
                arguments=[
                    Argument(
                        name="message",
                        description="what to say.",
                        usage_example="message=add buy milk to my todo",
                        help_content="(dani reference)",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    )
                ],
                subcommands={},
                binding=NativeBinding(handler=self.say_handler),
            )
        }

    def get_dry(self) -> dict[str, Command]:
        return {
            "dry": Command(
                name="dry",
                description="show what i would do, without actully doing it.",
                usage_example=">ai dry add buy milk to my todo",
                arguments=[
                    Argument(
                        name="message",
                        description="what you would have said.",
                        usage_example="message=add buy milk to my todo",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    )
                ],
                subcommands={},
                binding=NativeBinding(handler=self.dry_handler),
            )
        }

    def get_persona(self) -> dict[str, Command]:
        def leaf(name: str, description: str, handler, arguments=None) -> Command:
            return Command(
                name=name,
                description=description,
                arguments=arguments or [],
                subcommands={},
                binding=NativeBinding(handler=handler),
            )

        return {
            "persona": Command(
                name="persona",
                description="customize my personality for this channel.",
                arguments=[],
                binding=MockBinding(),
                subcommands={
                    "set": leaf(
                        "set",
                        "set this channel's persona.",
                        self.persona_set_handler,
                        [
                            Argument(
                                name="prompt",
                                description="the persona instructions.",
                                usage_example="prompt=you are gay",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "show": leaf(
                        "show", "show the current persona.", self.persona_get_handler
                    ),
                    "clear": leaf(
                        "clear",
                        "reset to my default persona (i.e. leech).",
                        self.persona_clear_handler,
                    ),
                },
            )
        }

    def get_clear(self) -> dict[str, Command]:
        return {
            "clear": Command(
                name="clear",
                description="reset this channel's conversation memory.",
                usage_example=">ai clear",
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.clear_handler),
            )
        }

    def get_on(self) -> dict[str, Command]:
        return {
            "on": Command(
                name="on",
                description="restart chatting here, after you stopped.",
                usage_example=">ai on",
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.on_handler),
            )
        }

    def get_off(self) -> dict[str, Command]:
        return {
            "off": Command(
                name="off",
                description="stop chatting here and forget this channel.",
                help_content="to reenable back use `>ai on`.",
                usage_example=">ai off",
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.off_handler),
            )
        }

    def get_status(self) -> dict[str, Command]:
        return {
            "status": Command(
                name="status",
                description="show my state in this channel.",
                usage_example=">ai status",
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.status_handler),
            )
        }

    def get_catalog(self) -> dict[str, Command]:
        return {
            "catalog": Command(
                name="catalog",
                description="show the commands i can see and run.",
                usage_example=">ai catalog",
                help_content="roughly what the model is shown: every visible command "
                "with its arguments and defaults.",
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.catalog_handler),
            )
        }

    def _visible(self, path: str, command: Command, user_id: int) -> bool:
        if command.ai_visible is not None:
            return command.ai_visible
        if command.elevated:
            return False
        if path == "ai.talk":
            return False
        if path.split(".")[0] == "sudo":
            return False
        if isinstance(command.binding, SlashBinding):
            return False
        if not is_invocable(command.binding):
            return False
        return self.context.client.security.decide_ai(
            user_id, f"command:{path}"
        ).allowed

    def catalog(self, user_id: int) -> list[schema.CatalogEntry]:
        return schema.build_catalog(
            self.context.client.commands,
            lambda path, command: self._visible(path, command, user_id),
        )

    def _where(self, message: discord.Message) -> str:
        channel = message.channel
        guild = getattr(channel, "guild", None)
        if guild is not None:
            return f"#{channel.name} of guild '{guild.name}'"  # pyright: ignore
        return "a direct message"

    async def on_message(self, message: discord.Message):
        client = self.context.client
        if message.author.bot:
            return False, None

        channel_id = message.channel.id
        if channel_id in self.ai.muted:
            return False, None  # `>ai off` — ghosted until `>ai on`
        addressed = self.is_addressed(message)

        if channel_id not in self.ai.active:
            if not addressed:
                return False, None
            self.ai.activate(channel_id)

        if not addressed:
            snippet = await self._reply_snippet(message)
            self.ai.observe(
                channel_id,
                message.author.display_name,
                message.author.id,
                message.content,
                reply=snippet,
            )
            return False, None

        content = message.content
        for form in (
            f"<@{client.user.id}>",
            f"<@!{client.user.id}>",
        ):  # pyright: ignore
            content = content.replace(form, " ")
        await self.request(message, content.strip() or "hi")
        return True, None

    def is_addressed(self, message: discord.Message) -> bool:
        client = self.context.client
        if client.user.mentioned_in(message):  # pyright: ignore
            return True
        ref = message.reference
        if ref is not None:
            resolved = ref.cached_message or getattr(ref, "resolved", None)
            if (
                isinstance(resolved, discord.Message)
                and resolved.author.id == client.user.id  # pyright: ignore
            ):  # pyright: ignore
                return True
        return False

    async def _reply_snippet(self, message: discord.Message) -> str | None:
        ref = message.reference
        if ref is None:
            return None
        resolved = ref.cached_message or getattr(ref, "resolved", None)
        if resolved is None and ref.message_id is not None:
            try:
                resolved = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        if (
            not isinstance(resolved, discord.Message) or not resolved.content.strip()
        ):  # pyright: ignore
            return None
        snippet = " ".join(resolved.content.split())[:120]  # pyright: ignore
        return f"{resolved.author.display_name}: {snippet}"  # pyright: ignore

    async def _gather_attachments(self, message: discord.Message) -> list[Attachment]:
        out: list[Attachment] = []
        seen: set[str] = set()

        for att in await gather_attachments(message):
            if len(out) >= MAX_ATTACHMENTS:
                break
            try:
                data = await att.read()
            except discord.HTTPException:
                continue
            seen.add(att.url)
            out.append(
                Attachment(
                    filename=att.filename or "attachment",
                    content_type=att.content_type,
                    size=att.size,
                    data=data,
                    viewable=(att.content_type or "").lower().startswith("image/"),
                )
            )

        targets = [message]
        ref = message.reference
        resolved = (
            (ref.cached_message or getattr(ref, "resolved", None))
            if ref is not None
            else None
        )
        if resolved is None and ref is not None and ref.message_id is not None:
            try:
                resolved = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                resolved = None
        if isinstance(resolved, discord.Message):
            targets.append(resolved)

        for target in targets:
            for url in URL_RE.findall(target.content or ""):
                if len(out) >= MAX_ATTACHMENTS:
                    break
                if url in seen or media_kind(url, None) != "image":
                    continue
                seen.add(url)
                fetched = await self._fetch_image(url)
                if fetched is not None:
                    data, content_type = fetched
                    out.append(
                        Attachment(
                            filename=url.split("/")[-1].split("?")[0] or "image",
                            content_type=content_type,
                            size=len(data),
                            data=data,
                            viewable=True,
                        )
                    )
        return out

    async def _fetch_image(self, url: str):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.content
                content_type = response.headers.get("content-type") or "image/png"
        except httpx.HTTPError:
            return None
        if len(data) > MAX_ATTACHMENT_BYTES:
            return None
        return data, content_type.split(";")[0].strip()

    async def request(
        self, message: discord.Message, content: str, *, dry: bool = False
    ) -> None:
        """v10.9 AI_OPSEC_2: one resolution at a time per channel. a second
        request while one runs gets deflected instead of interleaving two
        resolutions into the same context window."""
        channel_id = message.channel.id
        if not await self.ai.begin_resolution(channel_id):
            await self._reply(
                message,
                "i'm still working on the message before this one - "
                "give me a moment 😁",
            )
            return
        try:
            await self._request_inner(message, content, dry=dry)
        finally:
            self.ai.end_resolution(channel_id)

    async def _request_inner(
        self, message: discord.Message, content: str, *, dry: bool = False
    ) -> None:
        channel_id = message.channel.id
        system = schema.system_prompt(
            self.ai.persona(channel_id),
            self.catalog(message.author.id),
            where=self._where(message),
        )
        snippet = await self._reply_snippet(message)
        attachments = await self._gather_attachments(message)
        carried = content
        calls = 0
        summary: list[str] = []

        for round_index in range(MAX_ROUNDS):
            action: schema.AiAction | None = None
            feedback: str | None = None
            for _ in range(ATTEMPTS):
                if calls >= MODEL_CALL_BUDGET:
                    await self._reply(
                        message, "i hit my thinking budget for this message 😟"
                    )
                    # v10.9 AI_OPSEC_1: the model must be told this turn
                    # died, or the dangling prompt invites a retry later -
                    # possibly while answering someone else.
                    self.note(
                        channel_id,
                        "(the assistant hit its thinking budget and could not "
                        "answer the message above. do not retry it.)",
                    )
                    return
                calls += 1
                prompt = carried if feedback is None else feedback
                author = message.author.display_name if feedback is None else "(system)"
                try:
                    async with message.channel.typing():
                        raw = await self.ai.chat(
                            channel_id,
                            author,
                            message.author.id,
                            prompt,
                            system=system,
                            reply=snippet,
                            attachments=(
                                attachments
                                if round_index == 0 and feedback is None
                                else ()
                            ),
                        )
                except AiError as e:
                    self.log.write(
                        kind="error", principal=message.author.id, note=e.message
                    )
                    await message.reply(
                        f"i couldn't generate a proper response 😟 details: {e.message}"
                    )
                    # v10.9 AI_OPSEC_1: record the failure IN the window so
                    # the model knows that prompt already died once and
                    # must not be picked up again later - least of all
                    # while answering a different user.
                    self.note(
                        channel_id,
                        "(the assistant FAILED to answer the message above: "
                        f"{e.message[:200]}. it already errored out - do not "
                        "retry it, do not answer it on someone else's turn.)",
                    )
                    return
                try:
                    action = schema.AiAction.model_validate_json(
                        schema.strip_fences(raw)
                    )
                    break
                except ValidationError as e:
                    _logger.warning("ai output rejected: %.500s - error: %s", raw, e)
                    feedback = (
                        f"(system) your output was invalid for the schema: {e}. "
                        "respond with corrected JSON only."
                    )
            if action is None:
                self.log.write(
                    kind="error",
                    principal=message.author.id,
                    note="schema retries exhausted",
                )
                await self._reply(message, "i couldn't turn that into a command 😟")
                self.note(
                    channel_id,
                    "(the assistant could not produce a valid response for the "
                    "message above. do not retry it.)",
                )
                return

            if not action.commands:
                if dry:
                    await self._reply(
                        message,
                        f"```json\n{action.model_dump_json(indent=2)}\n```\n(dry run - nothing executed.)",
                    )
                    self.log.write(kind="dry", principal=message.author.id, note="chat")
                else:
                    await self._reply(message, action.reply or "…")
                return

            results: list[str] = []
            for index, requested in enumerate(action.commands):
                confirmation = requested.reply or (action.reply if index == 0 else None)
                if confirmation and not dry:
                    await self._reply(message, confirmation)

                command = self.context.client.commands.get_command(requested.command)
                if command is None:
                    results.append(f"(error) '{requested.command}' does not exist")
                    self.log.write(
                        kind="denied",
                        principal=message.author.id,
                        command=requested.command,
                        note="unknown command",
                    )
                    continue
                if not self._visible(requested.command, command, message.author.id):
                    results.append(
                        f"(error) '{requested.command}' is not available to you"
                    )
                    self.log.write(
                        kind="denied",
                        principal=message.author.id,
                        command=requested.command,
                        note="not in ai catalog",
                    )
                    continue

                # v10.9 AI_OPSEC_4: allow / deny / ask. anything not
                # explicitly allowed (or denied) gets confirmed with the
                # user right here, before anything runs.
                if (
                    not dry
                    and self._delegation_mode(requested.command, message.author.id)
                    == "ask"
                ):
                    verdict, remembered = await self._confirm_command(
                        message, requested.command
                    )
                    if remembered == "always":
                        self.context.client.security.allow_delegation(
                            message.author.id, f"command:{requested.command}"
                        )
                    elif remembered == "never":
                        self.context.client.security.deny_delegation(
                            message.author.id, f"command:{requested.command}"
                        )
                    if not verdict:
                        results.append(
                            f"(error) the user did not allow '{requested.command}' - "
                            "do not run it, do not ask again this turn. say so "
                            "and move on."
                        )
                        self.log.write(
                            kind="denied",
                            principal=message.author.id,
                            command=requested.command,
                            note="user declined (ask mode)",
                        )
                        continue

                try:
                    inner = Invocation.parse(
                        message=message,
                        command=command,
                        tokens=schema.action_tokens(requested),
                        chain=Chain(),
                        via="ai",
                    )
                except BaseError as e:
                    results.append(
                        f"(error) '{requested.command}' is invalid: {e.message}"
                    )
                    self.log.write(
                        kind="denied",
                        principal=message.author.id,
                        command=requested.command,
                        note=e.message,
                    )
                    continue

                if dry:
                    args = (
                        ", ".join(f"{k}={v!r}" for k, v in requested.arguments.items())
                        or "no arguments"
                    )
                    await self._reply(
                        message,
                        f"```json\n{action.model_dump_json(indent=2)}\n```\n-> would run: `{requested.command}` with: {args}\n(dry run - nothing executed.)",
                    )
                    self.log.write(
                        kind="dry",
                        principal=message.author.id,
                        command=requested.command,
                        arguments=requested.arguments,
                    )
                    return

                marker = discord.utils.time_snowflake(discord.utils.utcnow(), high=True)
                # v10.9 AI_OPSEC_3: bound the wait on one command. ytdlp or
                # tts runs take minutes - blocking the turn that long is
                # what starved the read path and bred retries/double
                # replies. past the bound the command keeps running in the
                # background and its output is noted into the window when
                # it lands.
                runner = asyncio.create_task(
                    self.context.client.dispatcher.run(message, inner)
                )
                timed_out = False
                try:
                    chain = await asyncio.wait_for(
                        asyncio.shield(runner), timeout=AI_CMD_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    chain = None

                if timed_out:
                    results.append(
                        f"(result) '{requested.command}' is still running in the "
                        f"background (it takes a while). tell the user it is in "
                        "progress and that its output will show up on its own - "
                        "do not run it again."
                    )
                    self.log.write(
                        kind="late",
                        principal=message.author.id,
                        command=requested.command,
                        arguments=requested.arguments,
                    )
                    self.context.spawn(
                        self._late_completion(
                            message, channel_id, runner, marker, requested.command
                        ),
                        name="ai-late-command",
                    )
                    continue

                outputs: list[str] = []
                if chain is not None and chain.out:
                    outputs = [str(entry) for entry in chain.out]
                else:
                    if chain is not None:
                        for execution in chain.executions:
                            harvested = await execution.settle()
                            if harvested is not None:
                                outputs.append(harvested.render() or "")
                    outputs += await self.collect_outputs(message, marker)
                results.extend(f"(result) {line}" for line in outputs if line)
                self.log.write(
                    kind="run",
                    principal=message.author.id,
                    command=requested.command,
                    arguments=requested.arguments,
                )

            summary.extend(results)
            carried = (
                "(system) command results:\n"
                + "\n".join(results)
                + "\nrespond with the answer to the user, or more commands "
                "if genuinely needed."
            )

        if calls < MODEL_CALL_BUDGET:
            calls += 1
            try:
                async with message.channel.typing():
                    raw = await self.ai.chat(
                        channel_id,
                        "(system)",
                        message.author.id,
                        "(system) you've used all your command rounds. reply to the "
                        "user now. no more commands, summarize what happened.",
                        system=system,
                    )
                final = schema.AiAction.model_validate_json(schema.strip_fences(raw))
                await self._reply(message, final.reply or "…")
                return
            except (AiError, ValidationError):
                pass
        await self._reply(
            message, "i ran out of rounds 😟\n" + "\n".join(summary[-12:])
        )
        self.note(
            channel_id,
            "(the assistant ran out of command rounds for the message above "
            "and summarized what it had. nothing is pending - do not resume it.)",
        )

    def _delegation_mode(self, path: str, user_id: int) -> str:
        """v10.9 AI_OPSEC_4: 'allow' | 'deny' | 'ask' for a command path."""
        return self.context.client.security.delegation_mode(user_id, f"command:{path}")

    async def _confirm_command(
        self, message: discord.Message, command: str
    ) -> tuple[bool, str | None]:
        """v10.9 AI_OPSEC_4: ask the user before an ask-mode command runs.
        returns (allowed, remembered) where remembered is None | 'always' |
        'never' - the caller persists the memory."""
        prompt = await self._reply(
            message,
            f"🤖 allow me to run `{command}` for you? reply:\n"
            "`yes` (this once) · `always` (remember) · `no` (this once) · "
            "`never` (remember the denial)",
        )
        session = ReplySession(
            self.context.client,
            message.channel.id,
            message.author.id,
            regex=CONFIRM_RE,
            target_id=prompt.id,
            name="ai-confirm",
        )
        try:
            reply = await session.next(timeout=AI_CONFIRM_TIMEOUT)
        finally:
            session.close()
        if reply is None:
            try:
                await prompt.reply(
                    "(no answer - taking that as a no.)", mention_author=False
                )
            except discord.HTTPException:
                pass
            return False, None
        word = (reply.content or "").strip().lower().split(" ", 1)[0]
        if word in CONFIRM_ALWAYS:
            return True, "always"
        if word in CONFIRM_NEVER:
            return False, "never"
        if word in CONFIRM_YES:
            return True, None
        return False, None

    async def _late_completion(
        self,
        message: discord.Message,
        channel_id: int,
        runner: asyncio.Task,
        marker_id: int,
        command: str,
    ) -> None:
        """v10.9 AI_OPSEC_3: a command that outlived AI_CMD_TIMEOUT finishes
        here, in the background. its output is noted into the window so the
        next turn knows it landed (observe() holds it back if another
        resolution is already running)."""
        try:
            await runner
            outputs = await self.collect_outputs(message, marker_id)
        except Exception:
            _logger.exception("ai late command watcher failed")
            return
        if not outputs:
            return
        joined = " | ".join(line.strip() for line in outputs if line.strip())
        self.ai.observe(
            channel_id,
            "(system)",
            0,
            f"(background command `{command}` finished) {joined[:600]}",
        )

    async def collect_outputs(
        self, message: discord.Message, marker_id: int
    ) -> list[str]:
        me = self.context.client.user.id  # pyright: ignore
        texts: list[str] = []
        after = discord.Object(id=marker_id)
        try:
            async for msg in message.channel.history(limit=15, after=after):
                if msg.author.id == me:
                    rendered = harvest(msg).render()
                    # v10.9 pages x ai: paginated command output carries a
                    # pager footer ("page 1/3 · reply: next / ...") - it is
                    # navigation chrome for humans, not command output, and
                    # the model reading it as data is how the pages system
                    # "collides" with the ai. strip it.
                    rendered = paginator.strip_footer(rendered)
                    if rendered:
                        texts.append(rendered)
        except discord.HTTPException:
            pass
        return texts

    async def _reply(self, message: discord.Message, text: str) -> discord.Message:
        return await message.reply(
            text[:REPLY_LIMIT],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def say_handler(self, invocation: Invocation):
        if invocation.via == "ai":
            raise CogError(
                "the assistant tried to talk to itself 😁", is_user_error=True
            )
        self.ai.activate(invocation.message.channel.id)
        await self.request(invocation.message, invocation.arguments["message"])
        return None

    async def dry_handler(self, invocation: Invocation):
        self.ai.activate(invocation.message.channel.id)
        await self.request(
            invocation.message, invocation.arguments["message"], dry=True
        )
        return None

    async def catalog_handler(self, invocation: Invocation):
        entries = self.catalog(invocation.message.author.id)
        lines: list[str] = []
        for entry in entries:
            args = ", ".join(
                f"{a.name}: {a.type}" + ("" if a.required else " (optional)")
                for a in entry.arguments
            )
            lines.append(
                f"`{entry.command}`"
                + (f" [{args}]" if args else "")
                + f" - {entry.description}"
            )
        payload = schema.catalog_prompt_block(entries)
        lines.append("")
        lines.append(
            f"> {len(entries)} commands, {len(payload)} chars "
            f"(~{len(payload) // 4} tokens)."
        )
        message = invocation.message
        await paginator.paginate(
            invocation.message, "\n".join(lines), client=self.context.client
        )

    async def status_handler(self, invocation: Invocation):
        channel_id = invocation.message.channel.id
        window = self.ai.window(channel_id)
        try:
            model_lines, daily = await self.ai.usage()
        except AiError as e:
            model_lines, daily = [f"  (unreachable: {e.message})"], {}
        lines = [
            "models:",
            *(f"  {line}" for line in model_lines),
            f"thinking: `{config.ai_thinking}` | turn budget: `{config.ai_turn_deadline}s` | service: {self.ai.service_name()}",
        ]
        if daily:
            lines.append(
                f"daily budget: {daily.get('used', 0)}/{daily.get('limit', 0)} chats"
            )
        lines += [
            f"persona: {'channel override' if channel_id in self.ai.personas else 'default'}",
            f"memory: {window.size()} messages, ~{window.chars()} chars (~{window.chars() // 4} tokens) of a {window.budget()} char budget",
            f"catalog: {len(self.catalog(invocation.message.author.id))} commands",
            f"active: {'yes' if channel_id in self.ai.active else 'no'}",
            f"muted: {'yes (use `>ai on`)' if channel_id in self.ai.muted else 'no'}",
        ]
        await invocation.message.reply("\n".join(lines))

    async def model_list_handler(self, invocation: Invocation):
        await invocation.message.reply(await self._chain_reply("model chain:"))

    async def model_add_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        provider, _, model = invocation.arguments["model"].partition(":")
        position = invocation.arguments.get("position")
        try:
            key = await self.ai.add_slot(provider.strip(), model.strip(), position)
        except AiError as e:
            raise CogError(
                f"i couldn't add that model. details: {e.message}", is_user_error=True
            ) from e
        await invocation.message.reply(
            await self._chain_reply(f"added `{key}`. chain:")
        )

    async def model_remove_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        try:
            await self.ai.remove_slot(invocation.arguments["model"].strip())
        except AiError as e:
            raise CogError(
                f"i couldn't remove that model. details: {e.message}",
                is_user_error=True,
            ) from e
        await invocation.message.reply(await self._chain_reply("removed. chain:"))

    async def model_main_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        try:
            await self.ai.promote_slot(invocation.arguments["model"].strip())
        except AiError as e:
            raise CogError(
                f"i couldn't promote that model. details: {e.message}",
                is_user_error=True,
            ) from e
        await invocation.message.reply(await self._chain_reply("promoted. chain:"))

    async def model_reset_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        await self.ai.reset_chain()
        await invocation.message.reply(
            await self._chain_reply("chain reset to the manager's default:")
        )

    async def model_budget_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        try:
            self.ai.set_window_budget(invocation.arguments["chars"])
        except ValueError as e:
            raise CogError(
                f"i couldn't set that budget. details: {e}", is_user_error=True
            ) from e
        chars = invocation.arguments["chars"]
        await invocation.message.reply(
            f"window budget set to {chars} chars (~{chars // 4} tokens)."
        )

    async def persona_set_handler(self, invocation: Invocation):
        channel_id = invocation.message.channel.id
        self.ai.set_persona(channel_id, invocation.arguments["prompt"])
        self.ai.window(channel_id).push(
            "(system)",
            0,
            "(the persona has been changed; ignore any earlier personality "
            "in this conversation and follow the new one.)",
        )
        await invocation.message.reply("persona set (history kept, change marked).")

    async def persona_get_handler(self, invocation: Invocation):
        await invocation.message.reply(self.ai.persona(invocation.message.channel.id))

    async def persona_clear_handler(self, invocation: Invocation):
        self.ai.window(invocation.message.channel.id).clear()
        self.ai.clear_persona(invocation.message.channel.id)
        await invocation.message.reply("persona reset to default.")

    async def clear_handler(self, invocation: Invocation):
        self.ai.window(invocation.message.channel.id).clear()
        await invocation.message.reply(
            "ok, but, its all history stoopid stoopid man 😢🇧🇷."
        )

    async def off_handler(self, invocation: Invocation):
        channel_id = invocation.message.channel.id
        self.ai.mute(channel_id)
        await invocation.message.reply("ok, i will ghost you.")

    async def on_handler(self, invocation: Invocation):
        channel_id = invocation.message.channel.id
        self.ai.unmute(channel_id)
        self.ai.activate(channel_id)
        await invocation.message.reply("ok, back to normal.")


def create_cog(context: CogContext):
    return AiCog(context=context)
