from __future__ import annotations

import discord
from pydantic import ValidationError

from leech.core.cog.cog import Cog
from leech.core.cog.context import CogContext, Priority
from leech.core.cog.error import CogError
from leech.core.command.invocation import Invocation
from leech.core.command.mock.binding import MockBinding
from leech.core.command.native.binding import NativeBinding
from leech.core.command.runtime import Argument, Command
from leech.core.command.slash.binding import SlashBinding
from leech.core.command.helpers import is_invocable
from leech.core.ai import schema
from leech.core.ai.service import AiError, AiService
import leech.config as config
from leech.core.error import BaseError
from leech.core.parser.types.definition import Types
from leech.core.parser.types.registry import TypesRegistry

REPLY_LIMIT = 1990
ATTEMPTS = 3


class AiCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.context.add_on_message(Priority.LOW, self.on_message)
        self.ai = AiService(api_key=config.ai_api_key) # pyright: ignore reason: validated

    async def load(self):
        ai = Command(
            name="ai",
            description="chat with the leech, it can also run commands for you.",
            usage_example=">ai say add buy milk to my todo",
            help_content=(
                "i will answers to mentions and replies, or `>ai talk`.\n"
                "when a request maps to a command i will try to run it on your behalf "
            ),
            arguments=[],
            subcommands=(self.get_talk() | self.get_persona() | self.get_clear()
                         | self.get_off() | self.get_status()),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(ai.name, ai):
            raise CogError(f"i failed to load '{ai.name}' command.")

    def get_talk(self) -> dict[str, Command]:
        return {"talk": Command(
            name="talk", description="talk to me directly.",
            usage_example=">ai talk what's on my todo list?",
            help_content="same as mentioning me, but explicit. i may answer in chat or run a command for you.",
            arguments=[Argument(
                name="message", description="what to say.",
                usage_example="message=add buy milk to my todo",
                help_content="(dani reference)",
                type=TypesRegistry.require(Types.STR), required=True)],
            subcommands={}, binding=NativeBinding(handler=self.say_handler))}

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
                                type=TypesRegistry.require(Types.STR), required=True)]),
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
            name="status", description="show the my state in this channel.",
            usage_example=">ai status", arguments=[], subcommands={},
            binding=NativeBinding(handler=self.status_handler))}

    def _visible(self, path: str, command: Command) -> bool:
        if path.split(".")[0] == "ai":
            return False
        if isinstance(command.binding, SlashBinding):
            return False
        return is_invocable(command.binding)

    def catalog(self) -> list[schema.CatalogEntry]:
        return schema.build_catalog(self.context.client.commands, self._visible)

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
            self.ai.window(channel_id).push(message.author.display_name, message.content)
            return False, None

        content = message.content
        for form in (f"<@{client.user.id}>", f"<@!{client.user.id}>"): # pyright: ignore
            content = content.replace(form, " ")
        inner = await self.request(message, content.strip() or "hi")
        return True, inner

    def is_addressed(self, message: discord.Message) -> bool:
        client = self.context.client
        if client.user.mentioned_in(message): # pyright: ignore
            return True
        ref = message.reference
        if ref is not None:
            resolved = ref.cached_message or getattr(ref, "resolved", None)
            if isinstance(resolved, discord.Message) and resolved.author.id == client.user.id: # pyright: ignore
                return True
        return False

    async def request(self, message: discord.Message, content: str, *,
                      chain=None) -> Invocation | None:
        system = schema.system_prompt(self.ai.persona(message.channel.id), self.catalog())
        feedback: str | None = None

        for _ in range(ATTEMPTS):
            prompt = content if feedback is None else feedback
            author = message.author.display_name if feedback is None else "(system)"
            try:
                async with message.channel.typing():
                    raw = await self.ai.chat(
                        message.channel.id, author, prompt, system=system)
            except AiError as e:
                await message.reply(f"i couldn't generate a proper response 😟 details: {e.message}")
                return None

            try:
                action = schema.AiAction.model_validate_json(schema.strip_fences(raw))
            except ValidationError as e:
                feedback = (f"(system) your output was invalid for the schema: {e}. "
                            "respond with corrected JSON only.")
                continue

            if action.command is None:
                await self._reply(message, action.reply or "…")
                return None

            command = self.context.client.commands.get_command(action.command)
            if command is None:
                feedback = (f"(system) command '{action.command}' does not exist. "
                            "use exact paths from the catalog; respond with "
                            "corrected JSON or a reply.")
                continue

            try:
                inner = Invocation.parse(
                        message=message, command=command,
                    tokens=schema.action_tokens(action),
                    chain=chain, via="ai")
            except BaseError as e:
                feedback = (f"(system) that invocation is invalid: {e.message}. "
                            "respond with corrected JSON or just a reply.")
                continue

            if action.reply:
                await self._reply(message, action.reply)
            return inner

        await message.reply("i couldn't turn that into a command 😟")
        return None

    async def _reply(self, message: discord.Message, text: str) -> None:
        await message.reply(text[:REPLY_LIMIT], mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())

    async def say_handler(self, invocation: Invocation):
        self.ai.activate(invocation.message.channel.id)
        return await self.request(
            invocation.message, invocation.arguments["message"],
            chain=invocation.chain)

    async def persona_set_handler(self, invocation: Invocation):
        self.ai.window(invocation.message.channel.id).clear()
        self.ai.set_persona(invocation.message.channel.id, invocation.arguments["prompt"])
        await invocation.message.reply("persona set for this channel and history cleared.")

    async def persona_get_handler(self, invocation: Invocation):
        await invocation.message.reply(self.ai.persona(invocation.message.channel.id))

    async def persona_clear_handler(self, invocation: Invocation):
        self.ai.window(invocation.message.channel.id).clear()
        self.ai.clear_persona(invocation.message.channel.id)
        await invocation.message.reply("persona reset to default.")

    async def clear_handler(self, invocation: Invocation):
        self.ai.window(invocation.message.channel.id).clear()
        await invocation.message.reply("but, its all history stoopid stoopid man 😢🇧🇷.")

    async def off_handler(self, invocation: Invocation):
        self.ai.deactivate(invocation.message.channel.id)
        await invocation.message.reply("ok, i'll forget this channel. mention me to start over.")

    async def status_handler(self, invocation: Invocation):
        channel_id = invocation.message.channel.id
        await invocation.message.reply("\n".join([
            f"model: `{self.ai.model}`",
            f"persona: {'channel override' if channel_id in self.ai.personas else 'default'}",
            f"memory: {self.ai.window(channel_id).size()} messages",
            f"active: {'yes' if channel_id in self.ai.active else 'no'}",
        ]))


def create_cog(context: CogContext):
    return AiCog(context=context)
