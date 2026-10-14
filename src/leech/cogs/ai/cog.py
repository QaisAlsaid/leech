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
from leech.core.ai.service import AiError, AiService
from leech.core.helpers.interactions import harvest
import leech.config as config
from leech.core.error import BaseError
from leech.core.parser.types.registry import Types, TypesRegistry
import leech.core.helpers.text.splitter as splitter

import json
import logging

_logger = logging.getLogger(__name__)

REPLY_LIMIT = 1990
ATTEMPTS = 3
MAX_ROUNDS = 3
MODEL_CALL_BUDGET = 4


class AiCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.context.add_on_message(Priority.LOW, self.on_message)
        self.ai = AiService(
            chain=config.ai_chain,
            keys=config.ai_keys,
            thinking=config.ai_thinking,
            read_timeout=config.ai_read_timeout,
            turn_deadline=config.ai_turn_deadline,
            window_limit=config.ai_window_limit,
            window_budget=config.ai_window_budget,
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
            subcommands=(self.get_talk() | self.get_dry() | self.get_persona()
                         | self.get_model() | self.get_catalog()
                         | self.get_clear() | self.get_off() | self.get_status()),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(ai.name, ai):
            raise CogError(f"i failed to load '{ai.name}' command.")

    # ... get_talk / get_dry / get_persona / get_model / get_catalog / get_clear /
    #     get_off / get_status: UNCHANGED from your current file — keep yours ...

    def get_model(self) -> dict[str, Command]:
        def leaf(name: str, description: str, handler, arguments=None) -> Command:
            return Command(name=name, description=description,
                           arguments=arguments or [], subcommands={},
                           binding=NativeBinding(handler=handler))
        return {"model": Command(
            name="model",
            description="view and reconfigure my model chain (owner only).",
            usage_example=">ai model list",
            help_content=(
                "the chain is ordered: the first model is the main, the rest are fallbacks "
                "tried in order. changes persist across restarts. `reset` returns to the "
                "startup default. `budget` sizes the context window, keep small for "
                "models with tight TPMs."
            ),
            arguments=[], binding=MockBinding(),
            subcommands={
                "list": leaf("list", "show the model chain.", self.model_list_handler),
                "add": leaf("add", "add a model to the chain.",
                            self.model_add_handler, [
                    Argument(name="model",
                             description="provider:model, e.g. groq:openai/gpt-oss-120b.",
                             usage_example="model=groq:openai/gpt-oss-20b",
                             type=TypesRegistry.require(Types.STR), required=True),
                    Argument(name="position", description="insert position (0 = main).",
                             usage_example="position=0",
                             type=TypesRegistry.require(Types.INT), required=False)]),
                "remove": leaf("remove", "remove a model from the chain.",
                               self.model_remove_handler, [
                    Argument(name="model", description="the provider:model to remove.",
                             usage_example="model=gemini:gemini-3.5-flash-lite",
                             type=TypesRegistry.require(Types.STR), required=True)]),
                "main": leaf("main", "promote a model to the top of the chain.",
                             self.model_main_handler, [
                    Argument(name="model", description="the provider:model to promote.",
                             usage_example="model=gemini:gemini-3.5-flash-lite",
                             type=TypesRegistry.require(Types.STR), required=True)]),
                "reset": leaf("reset", "restore the startup chain.",
                              self.model_reset_handler),
                "budget": leaf("budget", "set the context window size (chars).",
                               self.model_budget_handler, [
                    Argument(name="chars",
                             description="total rendered window budget in characters (~4 chars/token).",
                             usage_example="chars=8000",
                             type=TypesRegistry.require(Types.INT), required=True)]),
            })}

    def _require_owner(self, invocation: Invocation) -> None:
        if not is_owner(invocation.message.author.id):
            raise CogError("only owner touches my private parts 😁", is_user_error=True)

    def _chain_reply(self, prefix: str) -> str:
        lines = "\n".join(f"  {line}" for line in self.ai.usage())
        return f"{prefix}\n{lines}"
 
    # ... request() / on_message / is_addressed / _reply_snippet / _reply /
    #     say_handler / dry_handler / persona & clear & off handlers: exactly as
    #     delivered last round ...

    def get_talk(self) -> dict[str, Command]:
        return {"talk": Command(
            name="talk", description="talk to me directly.",
            usage_example=">ai talk what on my todo list?",
            help_content="same as mentioning me, but explicit. i may answer in chat or run a command for you.",
            arguments=[Argument(
                name="message", description="what to say.",
                usage_example="message=add buy milk to my todo",
                help_content="(dani reference)",
                type=TypesRegistry.require(Types.STR), required=True)],
            subcommands={}, binding=NativeBinding(handler=self.say_handler))}

    def get_dry(self) -> dict[str, Command]:
        return {"dry": Command(
            name="dry", description="show what i would do, without actully doing it.",
            usage_example=">ai dry add buy milk to my todo",
            arguments=[Argument(
                name="message", description="what you would have said.",
                usage_example="message=add buy milk to my todo",
                type=TypesRegistry.require(Types.STR), required=True)],
            subcommands={}, binding=NativeBinding(handler=self.dry_handler))}

    def get_persona(self) -> dict[str, Command]:
        def leaf(name: str, description: str, handler, arguments=None) -> Command:
            return Command(name=name, description=description,
                           arguments=arguments or [], subcommands={},
                           binding=NativeBinding(handler=handler))
        return {"persona": Command(
            name="persona",
            description="customize my personality for this channel.",
            arguments=[], binding=MockBinding(),
            subcommands={
                "set": leaf("set", "set this channel's persona.",
                            self.persona_set_handler, [Argument(
                                name="prompt", description="the persona instructions.",
                                usage_example="prompt=you are a pirate",
                                type=TypesRegistry.require(Types.STR), required=True),
                            Argument(
                                name="reset", description="clear history on change (default: yes).",
                                usage_example="reset=no",
                                help_content="set to 'no' to keep the conversation history. i'll mark the change instead.",
                                type=TypesRegistry.require(Types.BOOL), required=False)]),
                "show": leaf("show", "show the current persona.", self.persona_get_handler),
                "clear": leaf("clear", "reset to my default persona (i.e. leech).",
                              self.persona_clear_handler),
            })}

    def get_clear(self) -> dict[str, Command]:
        return {"clear": Command(
            name="clear", description="reset this channel's conversation memory.",
            usage_example=">ai clear", arguments=[], subcommands={},
            binding=NativeBinding(handler=self.clear_handler))}

    def get_off(self) -> dict[str, Command]:
        return {"off": Command(
            name="off", description="stop chatting here and forget this channel.",
            usage_example=">ai off",
            help_content="mention me again to start over with a clean memory.",
            arguments=[], subcommands={},
            binding=NativeBinding(handler=self.off_handler))}

    def get_status(self) -> dict[str, Command]:
        return {"status": Command(
            name="status", description="show my state in this channel.",
            usage_example=">ai status", arguments=[], subcommands={},
            binding=NativeBinding(handler=self.status_handler))}

    def get_catalog(self) -> dict[str, Command]:
        return {"catalog": Command(
            name="catalog", description="show the commands i can see and run.",
            usage_example=">ai catalog",
            help_content="roughly what i show the model: every visible command "
                         "with its arguments and defaults.",
            arguments=[], subcommands={},
            binding=NativeBinding(handler=self.catalog_handler))}
   
   # -- catalog (your policy, preserved; P4 makes it config-driven) --------------
    def _visible(self, path: str, command: Command) -> bool:
        if command.ai_visible is not None:
            return command.ai_visible
        if path == "ai.talk":           # reentrancy: no second model turn
            return False
        if isinstance(command.binding, SlashBinding):
            return False
        return is_invocable(command.binding)

    def catalog(self) -> list[schema.CatalogEntry]:
        return schema.build_catalog(self.context.client.commands, self._visible)

    def _where(self, message: discord.Message) -> str:
        channel = message.channel
        guild = getattr(channel, "guild", None)
        if guild is not None:
            return f"#{channel.name} of guild '{guild.name}'"
        return "a direct message"

    # -- listener -------------------------------------------------------------------
    async def on_message(self, message: discord.Message):
        client = self.context.client
        if message.author.bot:
            return False, None

        channel_id = message.channel.id
        addressed = self.is_addressed(message)

        if channel_id not in self.ai.active:
            if not addressed:
                return False, None
            self.ai.activate(channel_id)

        if not addressed:
            snippet = await self._reply_snippet(message)
            self.ai.window(channel_id).push(
                message.author.display_name, message.author.id,
                message.content, reply=snippet)
            return False, None

        content = message.content
        for form in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
            content = content.replace(form, " ")
        await self.request(message, content.strip() or "hi")
        return True, None   # commands now run inside request() via the front door

    def is_addressed(self, message: discord.Message) -> bool:
        client = self.context.client
        if client.user.mentioned_in(message):
            return True
        ref = message.reference
        if ref is not None:
            resolved = ref.cached_message or getattr(ref, "resolved", None)
            if (isinstance(resolved, discord.Message) 
                and resolved.author.id == client.user.id): # pyright: ignore
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
        if not isinstance(resolved, discord.Message) or not resolved.content.strip(): # pyright: ignore
            return None
        snippet = " ".join(resolved.content.split())[:120] # pyright: ignore
        return f"{resolved.author.display_name}: {snippet}" # pyright: ignore

    # -- the agent loop: emit -> run -> collect -> refeed -> reply --------------------
    async def request(self, message: discord.Message, content: str, *,
                      dry: bool = False) -> None:
        channel_id = message.channel.id
        system = schema.system_prompt(self.ai.persona(channel_id), self.catalog(),
                                      where=self._where(message))
        snippet = await self._reply_snippet(message)
        carried = content
        calls = 0
        summary: list[str] = []   # accumulated results across rounds (for wrap-up)

        for round_index in range(MAX_ROUNDS):
            # -- model turn (with schema-retry) --------------------------------
            action: schema.AiAction | None = None
            feedback: str | None = None
            for _ in range(ATTEMPTS):
                if calls >= MODEL_CALL_BUDGET:
                    await self._reply(message, "i hit my thinking budget for this message 😟")
                    return
                calls += 1
                prompt = carried if feedback is None else feedback
                author = message.author.display_name if feedback is None else "(system)"
                try:
                    async with message.channel.typing():
                        raw = await self.ai.chat(channel_id, author, message.author.id,
                                                 prompt, system=system, reply=snippet)
                except AiError as e:
                    self.log.write(kind="error", principal=message.author.id, note=e.message)
                    await message.reply(f"i couldn't generate a proper response 😟 details: {e.message}")
                    return
                try:
                    action = schema.AiAction.model_validate_json(schema.strip_fences(raw))
                    break
                except ValidationError as e:
                    _logger.warning("ai output rejected: %.500s — error: %s", raw, e)
                    feedback = (f"(system) your output was invalid for the schema: {e}. "
                                "respond with corrected JSON only.")
            if action is None:
                self.log.write(kind="error", principal=message.author.id,
                               note="schema retries exhausted")
                await self._reply(message, "i couldn't turn that into a command 😟")
                return

            # -- pure chat: done --------------------------------------------------
            if not action.commands:
                if dry:
                    await self._reply(message, f"```json\n{action.model_dump_json(indent=2)}\n```\n(dry run — nothing executed.)")
                    self.log.write(kind="dry", principal=message.author.id, note="chat")
                else:
                    await self._reply(message, action.reply or "…")
                return

            # -- execute commands, collect results ---------------------------------
            results: list[str] = []
            for index, requested in enumerate(action.commands):
                confirmation = requested.reply or (action.reply if index == 0 else None)
                if confirmation and not dry:
                    await self._reply(message, confirmation)

                command = self.context.client.commands.get_command(requested.command)
                if command is None:
                    results.append(f"(error) '{requested.command}' does not exist")
                    self.log.write(kind="denied", principal=message.author.id,
                                   command=requested.command, note="unknown command")
                    continue
                if not self._visible(requested.command, command):
                    results.append(f"(error) '{requested.command}' is not available to you")
                    self.log.write(kind="denied", principal=message.author.id,
                                   command=requested.command, note="not in ai catalog")
                    continue
                try:
                    inner = Invocation.parse(
                        message=message, command=command,
                        tokens=schema.action_tokens(requested),
                        chain=Chain(), via="ai")
                except BaseError as e:
                    results.append(f"(error) '{requested.command}' is invalid: {e.message}")
                    self.log.write(kind="denied", principal=message.author.id,
                                   command=requested.command, note=e.message)
                    continue

                if dry:
                    args = ", ".join(f"{k}={v!r}" for k, v in requested.arguments.items()) or "no arguments"
                    await self._reply(message, f"```json\n{action.model_dump_json(indent=2)}\n```\n→ would run: `{requested.command}` with: {args}\n(dry run — nothing executed.)")
                    self.log.write(kind="dry", principal=message.author.id,
                                   command=requested.command, arguments=requested.arguments)
                    return

                # marker AFTER the confirmation, BEFORE execution:
                # the sweep must not capture our own narration
                marker = discord.utils.time_snowflake(discord.utils.utcnow(), high=True)
                chain = await self.context.client.dispatcher.run(message, inner)
                outputs: list[str] = []
                if chain is not None and chain.out:
                    outputs = [str(entry) for entry in chain.out]      # structured first
                else:
                    outputs = await self.collect_outputs(message, marker)
                results.extend(f"(result) {line}" for line in outputs if line)
                self.log.write(kind="run", principal=message.author.id,
                               command=requested.command, arguments=requested.arguments)

            summary.extend(results)
            carried = ("(system) command results:\n" + "\n".join(results)
                       + "\nrespond with the answer to the user, or more commands "
                         "if genuinely needed.")

        # -- rounds exhausted: force a wrap-up reply ------------------------------
        if calls < MODEL_CALL_BUDGET:
            calls += 1
            try:
                async with message.channel.typing():
                    raw = await self.ai.chat(
                        channel_id, "(system)", message.author.id,
                        "(system) you've used all your command rounds. reply to the "
                        "user now — no more commands, summarize what happened.",
                        system=system)
                final = schema.AiAction.model_validate_json(schema.strip_fences(raw))
                await self._reply(message, final.reply or "…")
                return
            except (AiError, ValidationError):
                pass
        await self._reply(message, "i ran out of rounds 😟\n" + "\n".join(summary[-12:]))

    async def collect_outputs(self, message: discord.Message,
                              marker_id: int) -> list[str]:
        """post-run sweep of our own messages — captures every command's output,
        because everything a command shows the user is sent by leech."""
        me = self.context.client.user.id
        texts: list[str] = []
        after = discord.Object(id=marker_id)
        try:
            async for msg in message.channel.history(limit=15, after=after):
                if msg.author.id == me:
                    rendered = harvest(msg).render()
                    if rendered:
                        texts.append(rendered)
        except discord.HTTPException:
            pass
        return texts

    async def _reply(self, message: discord.Message, text: str) -> discord.Message:
        return await message.reply(text[:REPLY_LIMIT], mention_author=False,
                                    allowed_mentions=discord.AllowedMentions.none())

    # -- handlers ------------------------------------------------------------------
    async def say_handler(self, invocation: Invocation):
        if invocation.via == "ai":
            # the structural latch: via now survives derive hops, so this fires
            # even when the AI reaches ai.talk through cmd.invoke indirection.
            raise CogError("the assistant can't talk to itself 😁", is_user_error=True)
        self.ai.activate(invocation.message.channel.id)
        await self.request(invocation.message, invocation.arguments["message"])
        return None

    async def dry_handler(self, invocation: Invocation):
        self.ai.activate(invocation.message.channel.id)
        await self.request(invocation.message, invocation.arguments["message"], dry=True)
        return None

    
    async def catalog_handler(self, invocation: Invocation):
        entries = self.catalog()
        lines: list[str] = []
        for entry in entries:
            args = ", ".join(f"{a.name}: {a.type}" + ("" if a.required else " (optional)")
                             for a in entry.arguments)
            lines.append(f"`{entry.command}`"
                         + (f" [{args}]" if args else "")
                         + f" — {entry.description}")
        payload = schema.catalog_prompt_block(entries)
        lines.append("")
        lines.append(f"> {len(entries)} commands, {len(payload)} chars "
                     f"(~{len(payload) // 4} tokens).")
        message = invocation.message
        for chunk in splitter.split("\n".join(lines)):
            message = await message.reply(chunk)


    async def status_handler(self, invocation: Invocation):
        channel_id = invocation.message.channel.id
        window = self.ai.window(channel_id)
        await invocation.message.reply("\n".join([
            "models:",
            *(f"  {line}" for line in self.ai.usage()),
            f"thinking: `{config.ai_thinking}` | read timeout: `{config.ai_read_timeout}s` | turn budget: `{config.ai_turn_deadline}s`",
            f"persona: {'channel override' if channel_id in self.ai.personas else 'default'}",
            f"memory: {window.size()} messages, ~{window.chars()} chars (~{window.chars() // 4} tokens) of a {window.budget()} char budget",
            f"catalog: {len(self.catalog())} commands",
            f"active: {'yes' if channel_id in self.ai.active else 'no'}",
        ]))


    async def model_list_handler(self, invocation: Invocation):
        await invocation.message.reply(self._chain_reply("model chain:"))

    async def model_add_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        provider, _, model = invocation.arguments["model"].partition(":")
        position = invocation.arguments.get("position")
        try:
            slot = await self.ai.add_slot(provider.strip(), model.strip(), position)
        except ValueError as e:
            raise CogError(f"i couldn't add that model. details: {e}", is_user_error=True) from e
        await invocation.message.reply(self._chain_reply(f"added `{slot.key}`. chain:"))

    async def model_remove_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        try:
            await self.ai.remove_slot(invocation.arguments["model"].strip())
        except ValueError as e:
            raise CogError(f"i couldn't remove that model. details: {e}", is_user_error=True) from e
        await invocation.message.reply(self._chain_reply("removed. chain:"))

    async def model_main_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        try:
            await self.ai.promote_slot(invocation.arguments["model"].strip())
        except ValueError as e:
            raise CogError(f"i couldn't promote that model. details: {e}", is_user_error=True) from e
        await invocation.message.reply(self._chain_reply("promoted. chain:"))

    async def model_reset_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        await self.ai.reset_chain()
        await invocation.message.reply(self._chain_reply("chain reset to startup default:"))

    async def model_budget_handler(self, invocation: Invocation):
        self._require_owner(invocation)
        try:
            self.ai.set_window_budget(invocation.arguments["chars"])
        except ValueError as e:
            raise CogError(f"i couldn't set that budget. details: {e}", is_user_error=True) from e
        chars = invocation.arguments["chars"]
        await invocation.message.reply(
            f"window budget set to {chars} chars (~{chars // 4} tokens).")


    async def persona_set_handler(self, invocation: Invocation):
        channel_id = invocation.message.channel.id
        reset = invocation.arguments.get("reset", True)
        self.ai.set_persona(channel_id, invocation.arguments["prompt"])
        if reset:
            self.ai.window(channel_id).clear()
            await invocation.message.reply("persona set, history cleared.")
        else:
            self.ai.window(channel_id).push(
                "(system)", 0,
                "(the persona has been changed; ignore any earlier personality "
                "in this conversation and follow the new one.)")
            await invocation.message.reply("persona set (history kept, change marked).")

    async def persona_get_handler(self, invocation: Invocation):
        await invocation.message.reply(self.ai.persona(invocation.message.channel.id))

    async def persona_clear_handler(self, invocation: Invocation):
        self.ai.window(invocation.message.channel.id).clear()
        self.ai.clear_persona(invocation.message.channel.id)
        await invocation.message.reply("persona reset to default.")

    async def clear_handler(self, invocation: Invocation):
        self.ai.window(invocation.message.channel.id).clear()
        await invocation.message.reply("ok, but, its all history stoopid stoopid man 😢🇧🇷.")

    async def off_handler(self, invocation: Invocation):
        self.ai.deactivate(invocation.message.channel.id)
        await invocation.message.reply("ok, but, its all history stoopid stoopid man 😢🇧🇷.")




    # ... persona / model / clear / off / status / catalog handlers: UNCHANGED —
    #     keep yours, including the model group and its owner gate ...


def create_cog(context: CogContext):
    return AiCog(context=context)
