from __future__ import annotations

import discord

from leech.core.cog.cog import Cog
from leech.core.cog.context import CogContext, Priority
from leech.core.cog.error import CogError
from leech.core.command.invocation import Invocation
from leech.core.command.mock.binding import MockBinding
from leech.core.command.model import CommandDescriptor
from leech.core.command.native.binding import NativeBinding
from leech.core.command.runtime import Argument, Command
from leech.core.permissions import is_owner
from leech.core.provider.model import ProviderDescriptor, ProviderType
from leech.core.security import Decision
from leech.core.parser.types.registry import Types, TypesRegistry


class AuthCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.context.add_on_unload(self.unload)
        self.context.add_on_message(Priority.FIRST, self.gate)

    async def load(self):
        self.context.client.dispatcher.authorizer = self.check
        auth = Command(
            name="auth",
            description="manage authorization grants (owner only).",
            usage_example=">sudo auth allow user=@opposingfork scope=command:todo.*",
            help_content=(
                "scopes: 'command:todo.*' (prefix), 'command:*' (all commands), "
                "'message:*' (chat), '*' (everything). note: grants can never "
                "grant elevation. sudo stays owner only."
            ),
            arguments=[],
            subcommands=self.get_subcommands(),
            binding=MockBinding(),
            is_root=True,
            ai_visible=False,
            elevated=True
        )
        if not self.context.add_command(auth.name, auth):
            raise CogError(f"i failed to load '{auth.name}' command.")

        opsec = self.context.client.binder.bind(
            CommandDescriptor(
                name="opsec",
                description=auth.description,
                help_content=auth.help_content,
                usage_example=auth.usage_example,
                arguments=[],
                subcommands={},
                provider=ProviderDescriptor(type=ProviderType.SELF, target=[auth.name]),
                is_root=True,
            )
        )
        if not self.context.add_command(opsec.name, opsec):
            raise CogError(f"failed to load '{opsec.name}' self-command.")

    async def unload(self):
        self.context.client.dispatcher.authorizer = None

    async def gate(self, message: discord.Message):
        if message.author.bot:
            return False, None
        decision = self.context.client.security.decide_user(
            message.author.id, "message:*"
        )
        if not decision.allowed:
            return True, None
        return False, None

    async def check(self, message: discord.Message, invocation: Invocation) -> Decision:
        security = self.context.client.security
        command = invocation.command
        path = self.context.client.commands.path_of(command) or command.name
        scope = f"command:{path}"

        if command.elevated and not invocation.elevated:
            return Decision(
                False, f"'{path}' is privileged. run it as `>sudo {path} ...` 😁."
            )
        if invocation.elevated and invocation.via == "ai":
            return Decision(False, "the assistant can never elevate.")
        if invocation.elevated and not is_owner(message.author.id):
            return Decision(False, "elevation is owner only, always.")

        if invocation.via == "ai":
            return security.decide_ai(message.author.id, scope)
        return security.decide_user(message.author.id, scope)

    def get_subcommands(self) -> dict[str, Command]:
        user_arg = Argument(
            name="user",
            description="the user to configure.",
            usage_example="user=@opposingfork",
            type=TypesRegistry.require(Types.USER),
            required=True,
        )
        scope_arg = Argument(
            name="scope",
            description="the scope, e.g. command:todo.* or *.",
            usage_example="scope=command:todo.*",
            type=TypesRegistry.require(Types.STR),
            required=True,
        )
        return {
            "list": Command(
                name="list",
                description="list grants and delegations.",
                arguments=[],
                subcommands={},
                binding=NativeBinding(handler=self.list_handler),
            ),
            "allow": Command(
                name="allow",
                description="grant a user a scope.",
                arguments=[user_arg, scope_arg],
                subcommands={},
                binding=NativeBinding(handler=self.allow_handler),
                ai_visible=False,
                elevated=True
            ),
            "deny": Command(
                name="deny",
                description="deny a user a scope.",
                arguments=[user_arg, scope_arg],
                subcommands={},
                binding=NativeBinding(handler=self.deny_handler),
                ai_visible=False,
                elevated=True
            ),
            "clear": Command(
                name="clear",
                description="remove a user's grant for a scope.",
                arguments=[user_arg, scope_arg],
                subcommands={},
                binding=NativeBinding(handler=self.clear_handler),
                ai_visible=False,
                elevated=True
            ),
        }

    def _security(self):
        return self.context.client.security

    async def list_handler(self, invocation: Invocation):
        lines = [f"owners: {', '.join(f'<@{o}>' for o in _security_owners(self))}"]
        for grant in self._security().grants:
            state = "allow" if grant.allow else "deny"
            lines.append(
                f"`{grant.scope}`: {state} for <@{int(grant.actor.split(':')[1])}>"
            )
        for actor, entries in self._security().delegations.items():
            uid = int(actor.split(":")[1])
            for scope, allow in entries.items():
                state = "allowed" if allow else "denied"
                lines.append(f"delegation `{scope}`: {state} for <@{uid}>")
        await invocation.message.reply("\n".join(lines) or "no grants configured.")

    async def allow_handler(self, invocation: Invocation):
        self._owner_only(invocation)
        self._security().set_grant(
            invocation.arguments["user"], invocation.arguments["scope"], True
        )
        await invocation.message.reply("granted.")

    async def deny_handler(self, invocation: Invocation):
        self._owner_only(invocation)
        self._security().set_grant(
            invocation.arguments["user"], invocation.arguments["scope"], False
        )
        await invocation.message.reply("denied.")

    async def clear_handler(self, invocation: Invocation):
        self._owner_only(invocation)
        self._security().clear_grant(
            invocation.arguments["user"], invocation.arguments["scope"]
        )
        await invocation.message.reply("cleared.")

    def _owner_only(self, invocation: Invocation) -> None:
        if not is_owner(invocation.message.author.id):
            raise CogError("grants are owner only.", is_user_error=True)


def _security_owners(cog: "AuthCog") -> list[int]:
    return sorted(cog._security()._owners)


def create_cog(context: CogContext):
    return AuthCog(context=context)
