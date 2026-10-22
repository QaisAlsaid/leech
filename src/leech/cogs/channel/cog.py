from __future__ import annotations

import logging
import os
from typing import Any, Optional

import discord
import httpx

from leech.core.cog.cog import Cog
from leech.core.cog.context import CogContext
from leech.core.cog.error import CogError
from leech.core.command.mock.binding import MockBinding
from leech.core.command.native.binding import NativeBinding
from leech.core.command.runtime import Argument, Command
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry

_logger = logging.getLogger(__name__)

SUPERVISOR_URL = (
    (os.getenv("LEECH_SUPERVISOR_URL") or "http://127.0.0.1:8765").strip().rstrip("/")
)
SUPERVISOR_TOKEN = (os.getenv("LEECH_SUPERVISOR_TOKEN") or "").strip()
OWNER_IDS = [
    int(chunk)
    for chunk in (os.getenv("LEECH_OWNER_IDS") or "").split(",")
    if chunk.strip().isdigit()
]
REPLY_LIMIT = 1990


def _leaf(
    name: str, description: str, handler, arguments=None, elevated=False
) -> Command:
    return Command(
        name=name,
        description=description,
        arguments=arguments or [],
        subcommands={},
        binding=NativeBinding(handler=handler),
        elevated=elevated,
    )


def _group(name: str, description: str, subcommands: dict) -> Command:
    return Command(
        name=name,
        description=description,
        arguments=[],
        subcommands=subcommands,
        binding=MockBinding(),
    )


class ChannelCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.context.add_on_unload(self.shutdown)
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(200.0, connect=5.0))

    async def shutdown(self) -> None:
        await self._http.aclose()

    async def load(self):
        root = Command(
            name="channel",
            description="runtime channel control. stable and canary.",
            usage_example=">channel canary",
            help_content=(
                "switches the bot between the stable channel (master branch) and "
                "the canary channel (canary branch) without a panel "
                "restart. the supervisor checks the code out and respawns "
                "the app. a boot probe watches the new code for ~75s. if it "
                "dies, we auto-revert to the last good commit. "
                "`>channel revert` does that manually. `>channel pull` "
                "updates to the branch head. `>channel autopoll on/off` "
                "controls the automatic pulls.\n"
                "only the owner can use this."
            ),
            arguments=[],
            subcommands=(
                self._status_cmd()
                | self._stable_cmd()
                | self._canary_cmd()
                | self._pull_cmd()
                | self._revert_cmd()
                | self._autopoll_cmd()
            ),
            binding=MockBinding(),
            is_root=True,
            elevated=True,
        )
        if not self.context.add_command(root.name, root):
            raise CogError(f"i failed to load '{root.name}' command.")

    def _status_cmd(self) -> dict[str, Command]:
        return {
            "status": _leaf(
                "status",
                "what's running right now.",
                self.status_handler,
            ),
        }

    def _stable_cmd(self) -> dict[str, Command]:
        return {
            "stable": _leaf(
                "stable",
                "switch to the stable channel (master).",
                self.stable_handler,
                elevated=True,
            ),
        }

    def _canary_cmd(self) -> dict[str, Command]:
        return {
            "canary": _leaf(
                "canary",
                "switch to the canary channel (canary branch).",
                self.canary_handler,
                elevated=True,
            ),
        }

    def _pull_cmd(self) -> dict[str, Command]:
        return {
            "pull": _leaf(
                "pull",
                "pull the channel branch's head and restart.",
                self.pull_handler,
                [
                    Argument(
                        name="force",
                        description="on = pull even if the head is marked bad.",
                        usage_example="force=on",
                        type=TypesRegistry.require(Types.BOOL),
                        required=False,
                    )
                ],
                elevated=True,
            ),
        }

    def _revert_cmd(self) -> dict[str, Command]:
        return {
            "revert": _leaf(
                "revert",
                "back to the last commit that booted cleanly.",
                self.revert_handler,
                elevated=True,
            ),
        }

    def _autopoll_cmd(self) -> dict[str, Command]:
        return {
            "autopoll": _leaf(
                "autopoll",
                "turn the auto-pull loop on or off.",
                self.autopoll_handler,
                [
                    Argument(
                        name="value",
                        description="on or off.",
                        usage_example="value=off",
                        type=TypesRegistry.require(Types.BOOL),
                        required=True,
                    )
                ],
                elevated=True,
            ),
        }

    async def _reply(self, message: discord.Message, text: str) -> discord.Message:
        return await message.reply(
            text[:REPLY_LIMIT],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        headers = {"x-leech-token": SUPERVISOR_TOKEN} if SUPERVISOR_TOKEN else {}
        try:
            if method == "GET":
                response = await self._http.get(SUPERVISOR_URL + path, headers=headers)
            else:
                response = await self._http.post(
                    SUPERVISOR_URL + path, json=body or {}, headers=headers
                )
        except httpx.HTTPError as exc:
            raise CogError(
                f"the supervisor sidecar is unreachable "
                f"({type(exc).__name__}) - is the start command "
                "`python deploy/supervisor.py`?",
                is_user_error=True,
            )
        try:
            return response.json()
        except ValueError:
            raise CogError(
                f"the sidecar said {response.status_code} " "(non-json).",
                is_user_error=True,
            )

    def _describe(self, page: dict) -> str:
        parts = [
            f"channel: **{page.get('channel') or '?'}** "
            f"(`{page.get('branch') or '?'}`)"
        ]
        tag = page.get("describe") or ""
        if tag:
            parts.append(f"semver: `{tag}`")
        commit = page.get("commit") or ""
        if commit:
            parts.append(f"commit: `{str(commit)[:12]}`")
        alive = page.get("app_alive")
        parts.append(f"app: {'alive' if alive else 'DOWN'}")
        if page.get("app_uptime"):
            parts.append(f"uptime: {int(page['app_uptime']) // 60}m")
        if page.get("autopull") is not None:
            parts.append(f"autopull: {'on' if page.get('autopull') else 'off'}")
        if page.get("last_good"):
            parts.append(f"last good: `{str(page['last_good'])[:12]}`")
        bad = page.get("bad_commits") or []
        if bad:
            parts.append(f"bad: {', '.join(str(c)[:8] for c in bad)}")
        if page.get("last_error"):
            parts.append(f"last error: {page['last_error'][:120]}")
        return "\n".join(parts)

    async def status_handler(self, invocation: Invocation):
        page = await self._call("GET", "/")
        await self._reply(invocation.message, self._describe(page))

    async def stable_handler(self, invocation: Invocation):
        await self._reply(
            invocation.message,
            "switching to **stable** - checking out master "
            "and rebooting (probe ~75s)...",
        )
        result = await self._call("POST", "/switch", {"channel": "stable"})
        await self._reply(invocation.message, self._switch_reply(result))

    async def canary_handler(self, invocation: Invocation):
        await self._reply(
            invocation.message,
            "switching to **canary** - checking out the canary "
            "branch and rebooting (probe ~75s)...",
        )
        result = await self._call("POST", "/switch", {"channel": "canary"})
        await self._reply(invocation.message, self._switch_reply(result))

    def _switch_reply(self, result: dict) -> str:
        if not result.get("ok"):
            return f"switch failed - {result.get('error') or '?'}"
        lines = [
            f"switched to **{result.get('channel')}** "
            f"(`{result.get('branch')}`) at "
            f"`{str(result.get('commit') or '?')[:12]}`"
        ]
        if result.get("reverted_to"):
            lines.append(
                f"probe failed. auto-reverted to "
                f"`{str(result['reverted_to'])[:12]}`. the bad "
                f"commit `{str(result.get('bad_commit'))[:12]}` is "
                "blacklisted."
            )
        else:
            lines.append("probe passed. this commit is the new last-good.")
        return "\n".join(lines)

    async def pull_handler(self, invocation: Invocation):
        force = bool(invocation.arguments.get("force"))
        await self._reply(invocation.message, "pulling...")
        result = await self._call("POST", "/pull", {"force": force})
        if result.get("up_to_date"):
            await self._reply(
                invocation.message,
                f"already up to date (`{str(result.get('commit'))[:12]}`).",
            )
            return
        if not result.get("ok"):
            await self._reply(
                invocation.message, f"pull skipped. {result.get('error') or '?'}"
            )
            return
        if result.get("reverted_to"):
            await self._reply(
                invocation.message,
                f"pulled `{str(result.get('commit'))[:12]}` but the probe "
                f"failed. reverted to `{str(result['reverted_to'])[:12]}`.",
            )
            return
        await self._reply(
            invocation.message,
            f"pulled to `{str(result.get('commit'))[:12]}`. probe passed, "
            "running the new code.",
        )

    async def revert_handler(self, invocation: Invocation):
        result = await self._call("POST", "/revert", {})
        if result.get("already_there"):
            await self._reply(
                invocation.message,
                f"already on last good " f"`{str(result.get('commit'))[:12]}`.",
            )
            return
        if not result.get("ok"):
            await self._reply(
                invocation.message, f"revert failed - {result.get('error') or '?'}"
            )
            return
        await self._reply(
            invocation.message,
            f"reverted to `{str(result.get('commit'))[:12]}` and rebooted.",
        )

    async def autopoll_handler(self, invocation: Invocation):
        on = bool(invocation.arguments.get("value"))
        result = await self._call("POST", "/autopoll", {"on": on})
        state = "on" if result.get("autopoll") else "off"
        await self._reply(invocation.message, f"auto-pull is now {state}.")


def create_cog(context: CogContext):
    return ChannelCog(context=context)
