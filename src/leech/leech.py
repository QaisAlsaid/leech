import discord
import logging

import leech.core.parser.types
from leech.core.command.binder import CommandBinder
from leech.core.command.registry import CommandRegistry
from leech.core.command.dispatcher import CommandDispatcher
from leech.core.cog.manager import CogManager
from leech.core.error import BaseError
from leech.core.parser.types.registry import TypesRegistry
import leech.core.parser.lexer as lexer
import leech.core.log
from . import config

_logger = logging.getLogger(__name__)


class Leech(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.commands: CommandRegistry = CommandRegistry()
        self.binder: CommandBinder = CommandBinder(self.commands)
        self.dispatcher: CommandDispatcher = CommandDispatcher(self.commands, self)
        self.prefixes: list[str] = [">", "$"]
        self.mention_trigger = False
        self.cogs: CogManager = CogManager(client=self)

    async def setup_hook(self):
        try:
            await self.cogs.load("leech.cogs.ping.cog")
            await self.cogs.load("leech.cogs.sudo.cog")
            await self.cogs.load("leech.cogs.command.cog")
            await self.cogs.load("leech.cogs.todo.cog")
            await self.cogs.load("leech.cogs.help.cog")
            await self.cogs.load("leech.cogs.userphone.cog")
            await self.cogs.load("leech.cogs.ai.cog")
        except BaseError as e:
            _logger.critical(f"failed to load cog, details: {e.message}.")

    async def on_ready(self):
        _logger.info(f"logged in as {self.user} (id: {self.user.id})")

    async def on_message(self, message: discord.Message):
        if message.author.id == self.user.id:
            return

        _logger.debug(f"received message: {message.content}")
        content = message.content
        try:
            lexed = lexer.lex(
                content=content,
                prefixes=self.prefixes,
                bot_id=config.bot_id if self.mention_trigger else None,
                continue_on_no_trigger=False,
            )
            _logger.debug(
                f"lexed: trigger: {lexed.trigger}, prefix: {lexed.prefix}, args: {lexed.tokens}"
            )
            # if lexed.trigger != lexer.Triggers.NONE:
            consumed, result = await self.dispatcher.dispatch4(message, lexed.tokens)
            _logger.debug(f"command dispatched: consumed: {consumed}, result: {result}")
        except BaseError as e:
            await self.dispatcher.error(message, e.message, e.is_user_error)
