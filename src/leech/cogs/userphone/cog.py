from typing import cast

from leech.core.cog.cog import Cog
from leech.core.cog.error import CogError
from leech.core.command.runtime import Command, Argument
from leech.core.command.model import CommandDescriptor
from leech.core.command.invocation import Invocation
from leech.core.command.binder import CommandBinder
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.provider.runtime import Provider
from leech.core.command.native.binding import NativeBinding
from leech.core.error import BaseError
from leech.core.command.slash.binding import SlashBinding
from leech.core.command.mock.binding import MockBinding
from leech.core.cog.context import CogContext, Priority
import leech.core.command.slash.resolver as slash_resolver
import leech.core.command.slash.invoker as slash_invoker
from dataclasses import dataclass
from enum import Enum, auto
from leech.core.command.slash.execution import execute_slash

import asyncio
import discord


class DisplayMode(str, Enum):
    ANONYMOUS = "anonymous"
    USERNAME = "username"
    NICKNAME = "nickname"


class CallState(Enum):
    IDLE = auto()
    ACTIVE = auto()


@dataclass
class UserphoneSession:
    source_channel_id: int
    remote_channel_id: int
    target_bot_id: int
    call_command: slash_resolver.ResolvedSlashCommand
    hangup_command: slash_resolver.ResolvedSlashCommand
    display_mode: DisplayMode = DisplayMode.NICKNAME
    call_state: CallState = CallState.IDLE


class UserphoneCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.sessions: dict[int, UserphoneSession] = {}
        self.remote_channels: dict[int, int] = {}
        self.context.add_on_message(Priority.MEDIUM, self.on_message)

    async def load(self):
        userphone = Command(
            name="userphone",
            description="start a userphone session in a remote channel.",
            help_content="starts a userphone call where messages will be collected from the current channel and sent into the the remote channel and vice versa.",
            usage_example=">userphone remote=<#749353351530938370> type=call display_mode=nickname",
            arguments=[
                Argument(
                    name="remote",
                    description="the remote channel.",
                    usage_example="remote=<#749353351530938370>",
                    help_content="remote channel can either be from this current guild or completely different guild, as long as leech has required permissions in that channel, and the /userphone command is accessable.",
                    type=TypesRegistry.require(Types.CHANNEL),
                    required=True,
                ),
                Argument(
                    name="target",
                    description="the actual bot that implements userphone.",
                    usage_example="target=<@247283454440374274>",
                    help_content="attempted slash commands invocations will only target this bot, so as the message collection from remote",
                    type=TypesRegistry.require(Types.USER),
                    required=True,
                ),
                Argument(
                    name="call_command",
                    description="the slash command that will be used to start a call.",
                    usage_example="call_command=userphone",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                ),
                Argument(
                    name="hangup_command",
                    description="the slash command that will be used to hangup a call.",
                    usage_example="hangup_command=hangup",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                ),
                Argument(
                    name="display_mode",
                    description="the initial display mode of the session.",
                    usage_example="display_mode=nickname",
                    help_content="sets the initial display mode, valid values are: (nickname, username, anonymous), and it can be changed at any time. for more try `>help userphone display`.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
            ],
            subcommands=self.get_subcommands(),
            binding=NativeBinding(handler=self.userphone_handler),
            is_root=True,
            defaults={
                "target": 247283454440374274,
                "call_command": "userphone",
                "hangup_command": "hangup",
            },
        )
        if not self.context.add_command(userphone.name, userphone):
            raise CogError(f"failed to load '{userphone.name}' command")

    def get_subcommands(self) -> dict[str, Command]:
        return {
            **self.get_call(),
            **self.get_hangup(),
            **self.get_display(),
            **self.get_end(),
        }

    def get_call(self) -> dict[str, Command]:
        call = Command(
            name="call",
            description="start a call if not already started.",
            usage_example=">userphone call",
            help_content="start a userphone call, this will trigger provided `userphone` in the remote channel.\nthis command will resume the tunnel.",
            arguments=[],
            subcommands={},
            binding=NativeBinding(handler=self.call_handler),
        )
        return {call.name: call}

    def get_hangup(self) -> dict[str, Command]:
        hangup = Command(
            name="hangup",
            description="hangup a started call.",
            usage_example=">userphone hangup",
            help_content="hangup a userphone call, this will trigger provided `hangup` in the remote channel.\nthis command will pause the tunnel but keep the session open.",
            arguments=[],
            subcommands={},
            binding=NativeBinding(handler=self.hangup_handler),
        )
        return {hangup.name: hangup}

    def get_display(self) -> dict[str, Command]:
        display = Command(
            name="display",
            description="set the display mode for this seesion.",
            usage_example=">userphone display mode=anonymous",
            help_content="display mode sets how leech will show your name to the other side, since this is a fake userphone call, Yggdrasil will only show `leech_bot: <your_message>` so to overcome this leech formats the message in a way to show which user sent it, so for each display mode type the message will look to the other side as:\nnickname: `leech_bot: (<your_nickname> says): <your_message>`\nusername: `leech_bot: (<your_username> says) <your_message>`\nanonymous: `leech_bot: (anonymous#N says>) <your_message>` where in anonymous case the #N is anonymous id based on the number of particepents in call. this command requires started session for more on that try `>help userphone`.",
            arguments=[
                Argument(
                    name="mode",
                    description="the display mode to be set (could be either: nickname, username, anonymous).",
                    usage_example="mode=anonymous",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(handler=self.display_handler),
        )
        return {display.name: display}

    def get_end(self) -> dict[str, Command]:
        end = Command(
            name="end",
            description="end a started session.",
            usage_example=">userphone end",
            help_content="end a userphone session, this will trigger hangup if needed. and close the tunnel between this channel and remote channel.\nthis command requires a started call for more on that try `>help userphone call`.",
            arguments=[],
            subcommands={},
            binding=NativeBinding(handler=self.end_handler),
        )
        return {end.name: end}


    async def format_message_from_source(
    self,
    message: discord.Message,
    display_mode: DisplayMode,
) -> str:
        import re
        content = message.content.strip()
        content = re.sub(
            r"nigger",
            "nigga",
            content,
            flags=re.IGNORECASE,
        )

        formatted = f"({message.author.display_name} says): {content}"

        if message.reference is not None:
            referenced = message.reference.cached_message

            if referenced is None and message.reference.message_id is not None:
                try:
                    referenced = await message.channel.fetch_message(
                        message.reference.message_id
                    )
                except discord.HTTPException:
                    referenced = None

            if referenced is not None and referenced.content.strip():
                formatted += (
                    f"\n> was replying to {referenced.author.display_name}:\n"
                    + "\n".join(
                        f"> {line}"
                        for line in referenced.content.strip().splitlines()
                    )
                )

        if len(formatted) > 2000:
            raise BaseError("message content too long limit is 2000.")

        return formatted


    async def on_message_from_source(self, message: discord.Message, sesh: UserphoneSession):
        if sesh.call_state != CallState.ACTIVE or message.author.bot:
            return False, None
        content = message.content.strip()
        if content.startswith("\\"):
            return False, None
        remote = await self.ensure_channel(sesh.remote_channel_id)
        await self.validate_remote_channel(remote)
        await remote.send( # pyright: ignore reason: validated
                await self.format_message_from_source(message, sesh.display_mode))
        return True, None

    async def on_message_from_remote(self, message: discord.Message, sesh: UserphoneSession):
        if sesh.call_state != CallState.ACTIVE or message.author.id != sesh.target_bot_id:
            return False, None
        content = message.content.strip()
        source = await self.ensure_channel(sesh.source_channel_id)
        self.validate_source_channel(source)
        import re
        
        pattern = r"<a?:userphone(?:_\w+)?:\d+>|:userphone(?:_\w+)?:"
        content = re.sub(pattern, "📞", content).strip()

        if content:
            await source.send(content) # pyright: ignore reason validated
        return True, None

    async def on_message(self, message: discord.Message):
        sesh = self.sessions.get(message.channel.id)
        if sesh is not None:
            return await self.on_message_from_source(message, sesh)
        
        remote = self.remote_channels.get(message.channel.id)
        if remote is not None:
            sesh = self.sessions.get(remote)
            if sesh is not None:
                return await self.on_message_from_remote(message, sesh)

        return False, None

    async def userphone_handler(self, invocation: Invocation):
        remote = invocation.arguments["remote"]
        display = invocation.arguments.get("display_mode")
        target = invocation.arguments["target"]
        call = invocation.arguments["call_command"]
        hangup = invocation.arguments["hangup_command"]
        try:
            display = (
                DisplayMode(str(display).lower())
                if display is not None
                else DisplayMode.NICKNAME
            )
        except ValueError as e:
            raise CogError(
                f"invalid display mode, details: {e}, try `>help userphone display_mode`.",
                is_user_error=True,
            ) from e
        remote_channel = await self.validate_new_session(
            invocation.message.channel, remote, target, call, hangup
        )
        call_command = await slash_resolver.resolve_target(
            remote_channel, call.split(), target
        )
        hangup_command = await slash_resolver.resolve_target(
            remote_channel, hangup.split(), target
        )
        self.sessions[invocation.message.channel.id] = UserphoneSession(
            source_channel_id=invocation.message.channel.id,
            remote_channel_id=remote,
            call_state=CallState.IDLE,
            display_mode=display,
            target_bot_id=target,
            call_command=call_command,
            hangup_command=hangup_command,
        )
        self.remote_channels[remote] = invocation.message.channel.id
        await invocation.message.reply(
            f"userphone session started, a tunnel to <#{remote}> has been established."
        )

    async def call_handler(self, invocation: Invocation):
        sesh = self.ensure_session(invocation.message.channel.id)
        sesh.call_state = CallState.ACTIVE

        remote = await self.ensure_channel(sesh.remote_channel_id)
        await self.validate_remote_channel(remote)
        execution = await execute_slash(
            self.context.client, sesh.call_command, remote, {},
            application_id=sesh.target_bot_id,
            command_name=" ".join(sesh.call_command.target))
        harvested = await execution.settle()
        await invocation.message.reply(
            (harvested.render() if harvested else None)
            or "i received a response, but couldn't read it 😟")
        return None

    async def hangup(self, message: discord.Message, sesh: UserphoneSession):
        remote = await self.ensure_channel(sesh.remote_channel_id)
        await self.validate_remote_channel(remote)
        execution = await execute_slash(
            self.context.client, sesh.hangup_command, remote, {},
            application_id=sesh.target_bot_id,
            command_name=" ".join(sesh.hangup_command.target))
        harvested = await execution.settle()
        await message.reply(
            (harvested.render() if harvested else None)
            or "i received a response, but couldn't read it 😟")      

    async def hangup_handler(self, invocation: Invocation):
        sesh = self.ensure_session(invocation.message.channel.id)
        sesh.call_state = CallState.IDLE

        await self.hangup(invocation.message, sesh)
         

    async def display_handler(self, invocation: Invocation):
        invocation.arguments["mode"]

    async def end_handler(self, invocation: Invocation):
        sesh = self.ensure_session(invocation.message.channel.id)
        err = None
        try:
            remote = await self.ensure_channel(sesh.remote_channel_id)
            await self.hangup(invocation.message, sesh)
        except Exception as e:
            err = e

        self.sessions.pop(invocation.message.channel.id, None)
        remote = self.remote_channels.pop(sesh.remote_channel_id, None)
        await invocation.message.reply(f"closed session between the current channel and remote: <#{sesh.remote_channel_id}>{f' but i encountered an error during hangup, details: {err}' if err is not None else ''}.")

    def ensure_session(self, current: int) -> UserphoneSession:
        sesh = self.sessions.get(current)
        if sesh is None:
            raise CogError("no active session. maybe try starting a session via `>userphone` 😁.", is_user_error=True)
        return sesh

    async def fetch_channel(self, channle: int):
        try:
            actual = await self.context.client.fetch_channel(channle)
            return actual
        except discord.NotFound:
            raise CogError(
                "remote channel not found. make sure it is a valid channel id 😁.",
                is_user_error=True,
            )
        except discord.Forbidden:
            raise CogError(
                "i'm forbidden from accessing remote channel. make sure i'm added there 😁.",
                is_user_error=True,
            )

    def validate_source_channel(self, channel):
        botid = self.context.client.user.id  # pyright: ignore idk why it flags this?
        if isinstance(channel, discord.abc.GuildChannel):
            bot_member = channel.guild.get_member(botid)
            if bot_member is None:
                raise CogError(
                    f"i'm not a memeber of current channel: <#{channel.id}> 😟"
                )
            permissions = channel.permissions_for(bot_member)
            if not (permissions.send_messages and permissions.read_messages):
                raise CogError(
                    f"i don't have sufficient permissions in source channel. i require: (send messages and read messages permissions)."
                )

    
    async def validate_remote_channel(self, channel):
        if not isinstance(channel, discord.abc.GuildChannel):
            raise CogError(
                f"remote channel is not a in a guild.",
                is_user_error=True,
            )

        if not isinstance(channel, discord.abc.Messageable):
            raise CogError(
                f"remote channel is not a messageable channel.",
                is_user_error=True,
            )


        botid = self.context.client.user.id  # pyright: ignore
        bot_member = channel.guild.get_member(botid)
        if bot_member is None:
            raise CogError(
                f"i'm not a memeber of remote channel: <#{channel.id}>",
                is_user_error=True,
            )
        permissions = channel.permissions_for(bot_member)
        if not (
            permissions.send_messages
            and permissions.read_messages
            and permissions.use_application_commands
        ):
            raise CogError(
                f"i don't have sufficient permissions in remote channel: i require: (send messages and read messages and run application commands permissions).",
                is_user_error=True,
            )

    async def ensure_channel(self, channel_id: int):
        channel = self.context.client.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel


    async def validate_new_session(
        self,
        current: discord.abc.Messageable,
        remote: int,
        target: int,
        call: str,
        hangup: str,
    ) -> discord.abc.Messageable:
        snowflake = cast(discord.abc.Snowflake, current)
        sesh = self.sessions.get(snowflake.id)
        if sesh is not None:
            raise CogError(
                "the current channel already has a started session try stopping it or use the command in another channel.",
                is_user_error=True,
            )
        if snowflake.id == remote:
            raise CogError("the source channel id can't be same as remote channel id.")
        rem = self.remote_channels.get(remote)
        if rem is not None:
            raise CogError(
                f"the provided remote channel is already a remote channel for another connection either try using another channel or end the other session at: <#{rem}>",
                is_user_error=True,
            )
        rem = self.sessions.get(remote)
        if rem is not None:
            raise CogError(
                f"the provided remote already has a started channel for another connection either try using another channel or end the other session at: <#{remote}>",
                is_user_error=True,
            )
        remote_channel = await self.ensure_channel(remote)
        self.validate_source_channel(current)
        await self.validate_remote_channel(remote_channel)
        return remote_channel  # pyright: ignore reason: validated


def create_cog(context: CogContext):
    return UserphoneCog(context=context)
