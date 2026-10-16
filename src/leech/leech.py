import discord
import logging

import leech.core.parser.types
from leech.core.command.binder import CommandBinder
from leech.core.command.registry import CommandRegistry
from leech.core.command.dispatcher import CommandDispatcher
from leech.core.cog.manager import CogManager
from leech.core.error import BaseError
from leech.core.parser.types.registry import (
    TypesRegistry,
)
import leech.core.parser.lexer as lexer
import leech.core.log
from . import config
from leech.core.security import Security

_logger = logging.getLogger(__name__)

COGS = [
    "leech.cogs.auth.cog",  # always first
    "leech.cogs.ping.cog",
    "leech.cogs.sudo.cog",
    "leech.cogs.command.cog",
    "leech.cogs.todo.cog",
    "leech.cogs.help.cog",
    "leech.cogs.userphone.cog",
    "leech.cogs.image-compress.cog",
    "leech.cogs.ai.cog",  # always last
]


class Leech(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.commands: CommandRegistry = CommandRegistry()
        self.binder: CommandBinder = CommandBinder(self.commands)
        self.dispatcher: CommandDispatcher = CommandDispatcher(self.commands, self)
        self.prefixes: list[str] = config.prefixes
        self.mention_trigger = False
        self.cogs: CogManager = CogManager(client=self)
        self.security: Security = Security(config.owner_ids)

    async def setup_hook(self):
        for path in COGS:
            try:
                await self.cogs.load(path)
            except BaseError as e:
                _logger.critical(f"failed to load cog: '{path}', details: {e.message}.")
            except Exception:
                _logger.exception(f"failed to load cog: '{path}'.")

    async def on_ready(self):
        _logger.info(f"logged in as {self.user} (id: {self.user.id})")

    async def on_message(self, message: discord.Message):
        if message.author.id == self.user.id:
            return

        _logger.debug(
            f"received message: {message.content!r} author={message.author} "
            f"embeds={len(message.embeds)} attachments={len(message.attachments)}"
        )
        try:
            if message.author.bot:
                consumed, result = await self.dispatcher.dispatch(message, [])
            else:
                lexed = lexer.lex(
                    content=message.content,
                    prefixes=self.prefixes,
                    bot_id=config.bot_id if self.mention_trigger else None,
                    continue_on_no_trigger=False,
                )
                _logger.debug(
                    f"lexed: trigger: {lexed.trigger}, prefix: {lexed.prefix}, "
                    f"args: {lexed.tokens}"
                )
                consumed, result = await self.dispatcher.dispatch(message, lexed.tokens)
            _logger.debug(f"command dispatched: consumed: {consumed}, result: {result}")
        except BaseError as e:
            await self.dispatcher.error(message, e.message, e.is_user_error)
        except Exception:
            _logger.exception(f"unhandled error processing message: {message.id}")
