import asyncio
import logging
import re

import discord

from leech.core.cog.cog import Cog, CogContext
from leech.core.cog.error import CogError
from leech.core.command.runtime import Command, Argument
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.command.native.binding import NativeBinding
from leech.core.command.mock.binding import MockBinding
from leech.core.error import BaseError
from leech.core.helpers.interactions.reply import ReplySession
import leech.core.helpers.text.paginator as paginator
import time
from .service import (
    Perms,
    TaskOrigin,
    TodoService,
    UNDO_KEEP,
    UNSET,
)
from . import due as due_mod

_logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000

TASK_PREVIEW = 25

PRIORITY_EMOTE = {"high": "🔴", "medium": "🟠", "low": "🟡"}
PRIORITY_ALIASES = {
    "high": "high",
    "h": "high",
    "medium": "medium",
    "med": "medium",
    "m": "medium",
    "low": "low",
    "l": "low",
}
PRIORITY_CLEAR = {"none", "no", "null", "clear", "-"}

REMINDER_SCAN = 30.0
REMINDER_PROMPT_TIMEOUT = 600.0
REMINDER_REPLY_RE = re.compile(
    r"""^\s*(?:
        (?:done|d|finish|complete|delete|del|remove)\b
      | (?:snooze|later)\b
      | (?:ignore|skip|stop|cancel|keep|dismiss)\b
      | (?:in\s+)?\d+(?:\.\d+)?\s*[smhdw]\b
      | \d{1,2}:\d{2}
      | \d{1,2}\s*(?:am|pm)\b
      | (?:at\s+|@\s*)?(?:today|tonight|tomorrow|mon(?:day)?|tue(?:s?day)?|wed(?:nesday)?|thu(?:r?s?day)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b
      | \d{4}-\d{1,2}-\d{1,2}
      | \d{1,2}-\d{1,2}
    )""",
    re.IGNORECASE | re.VERBOSE,
)

DONE_WORDS = {"done", "d", "finish", "complete", "delete", "del", "remove"}
IGNORE_WORDS = {"ignore", "skip", "stop", "cancel", "keep", "dismiss"}
SNOOZE_WORDS = {"snooze", "later"}


def task_preview(task: str) -> str:
    return f"{task if len(task) < TASK_PREVIEW else f'{task[:TASK_PREVIEW - 3]}...'}"


def format_perms(perms: Perms):
    return f"{'r' if perms.read else ''}{'w' if perms.write else ''}{'d' if perms.remove else ''}"


def parse_perms(value: str | None) -> Perms:
    if value is None:
        return Perms()

    if any(char not in "rwd" for char in value):
        raise CogError("permissions must contain only r, w, and d.", is_user_error=True)

    if len(set(value)) != len(value):
        raise CogError("duplicate permissions are not allowed.", is_user_error=True)

    return Perms(
        read="r" in value,
        write="w" in value,
        remove="d" in value,
    )


class TodoCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.todo: TodoService = TodoService()
        self.command: Command

    async def load(self):
        todo = Command(
            name="todo",
            description="todo list commands.",
            help_content="commands to manage todo list(s).",
            arguments=[],
            subcommands=self.get_subcommands(),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(todo.name, todo):
            raise CogError(f"failed to load '{todo.name}' command.")
        self.command = todo

        remind = Command(
            name="remind",
            description="set a one-shot reminder - leech pings you when the time is up.",
            usage_example='>remind add call mom due="tomorrow 9am"',
            help_content=(
                "reminders live on their own 'reminders' list - your "
                "active list is never touched. when the time is up you "
                "get pinged here (dm fallback if unreachable): reply "
                "`done` to finish, `snooze 30m` (or a new time) to push "
                "it, or `ignore` to keep it. `>remind list` shows what is "
                "pending. it is the same machinery as `>todo` due dates - "
                "just a thinner door into it."
            ),
            arguments=[],
            subcommands=self.get_remind_subcommands(),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(remind.name, remind):
            raise CogError(f"failed to load '{remind.name}' command.")
        self.context.spawn(self._due_loop(), name="todo-due-loop")

    def get_subcommands(self) -> dict[str, Command]:
        return {
            **self.get_add(),
            **self.get_add_all(),
            **self.get_remove(),
            **self.get_update(),
            **self.get_list(),
            **self.get_create(),
            **self.get_discard(),
            **self.get_editros(),
            **self.get_set_editor(),
            **self.get_remove_editor(),
            **self.get_contributions(),
            **self.get_lists(),
            **self.get_activate(),
            **self.get_deactivate(),
            **self.get_tz(),
            **self.get_exclude(),
            **self.get_include(),
            **self.get_undo(),
            **self.get_log(),
            **self.get_backup(),
            **self.get_restore(),
        }

    def get_undo(self) -> dict[str, Command]:
        undo = Command(
            name="undo",
            description="undo the last change to your lists (whoever made it).",
            usage_example=">todo undo",
            help_content=(
                "restores your lists to how they were before the most "
                "recent mutation. even if the ai or another editor made "
                "it. undo again to walk back another step (the last "
                f"{UNDO_KEEP} steps are kept). once you make a new edit, the "
                "undone path is gone. (there is no redo)."
            ),
            arguments=[],
            subcommands={},
            binding=NativeBinding(self.undo_handler),
        )
        return {undo.name: undo}

    def get_log(self) -> dict[str, Command]:
        log = Command(
            name="log",
            description="see who changed your lists, and when.",
            usage_example=">todo log",
            help_content=(
                "the last mutations on your lists (by you, by editors, or "
                "by the ai acting through the dispatcher). newest first. "
            ),
            arguments=[
                Argument(
                    name="limit",
                    description="how many entries to show (default 10).",
                    usage_example="limit=25",
                    type=TypesRegistry.require(Types.INT),
                    required=False,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.log_handler),
        )
        return {log.name: log}

    def get_backup(self) -> dict[str, Command]:
        backup = Command(
            name="backup",
            description="snapshot everyone's todo data right now.",
            usage_example=">todo backup",
            help_content=(
                "writes a full copy of todo.json into the backups folder "
                "(the newest 12 are kept, one also is made automatically "
                "every 25 mutations). restore them with `>todo restore`."
            ),
            arguments=[],
            subcommands={},
            binding=NativeBinding(self.backup_handler),
            elevated=True,
        )
        return {backup.name: backup}

    def get_restore(self) -> dict[str, Command]:
        restore = Command(
            name="restore",
            description="list backups, or load one (owner only).",
            usage_example=">todo restore id=1",
            help_content=(
                "without id: lists the saved backups, newest last. with "
                "id: replaces all users' todo data with that backup "
                "(that's why it's owner only). the replace itself is "
                "undo protected (i.e. it snapshots first... for every user, careful!)."
            ),
            arguments=[
                Argument(
                    name="id",
                    description="which backup to load (from `>todo restore`).",
                    usage_example="id=2",
                    type=TypesRegistry.require(Types.INT),
                    required=False,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.restore_handler),
            elevated=True,
        )
        return {restore.name: restore}

    def get_add(self) -> dict[str, Command]:
        add = Command(
            name="add",
            description="add a task to the current active list.",
            usage_example='>todo add priority=high due="tomorrow 9am" finish writing forkic novel!',
            help_content=(
                "priority and due are optional. priority is high, "
                "medium or low (shows 🔴 🟠 🟡 when listed. tasks without one "
                "are shown last). due accepts 30m, 2h, 1d6h, 15:00, 3:30pm, "
                "today 15:00, tonight, tomorrow 9am, friday 3pm, "
                "2026-10-24 15:00... when the time is up you get pinged "
                "in this channel (or dm fallback if unreachable) and can snooze or done or ignore."
            ),
            arguments=[
                Argument(
                    name="task",
                    description="the task you wish to add.",
                    usage_example="task=finish writing forkic novel.",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                ),
                Argument(
                    name="priority",
                    description="task priority: high, medium or low.",
                    usage_example='priority="high"',
                    help_content="no priority is the old behavior: it sorts last. none clears it.",
                    type=TypesRegistry.require(Types.WORD),
                    required=False,
                ),
                Argument(
                    name="due",
                    description="when this task is due (30m, 15:00, tomorrow 9am, 10-24 ...).",
                    usage_example='due="tomorrow 9am"',
                    help_content="times read in as LEECH_TZ (default UTC). none clears it.",
                    type=TypesRegistry.require(Types.WORD),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.add_handler),
        )
        return {add.name: add}

    def get_add_all(self) -> dict[str, Command]:
        add_all = Command(
            name="adds",
            description="add '\\' separated tasks to the current active list.",
            usage_example='>todo adds tasks=a\\b\\c priority=high due="1d"',
            arguments=[
                Argument(
                    name="tasks",
                    description="the tasks you wish to add ('\\' separated).",
                    usage_example="tasks=finish writing forkic novel.\\draw daily bird",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                ),
                Argument(
                    name="priority",
                    description="priority applied to every task in this batch.",
                    usage_example='priority="high"',
                    type=TypesRegistry.require(Types.WORD),
                    required=False,
                ),
                Argument(
                    name="due",
                    description="due time applied to every task in this batch.",
                    usage_example='due="1d"',
                    type=TypesRegistry.require(Types.WORD),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.adds_handler),
        )
        return {add_all.name: add_all}

    def get_remove(self) -> dict[str, Command]:
        remove = Command(
            name="remove",
            description="remove a task from the current active list.",
            usage_example="todo remove index=2",
            help_content="by removing a task you remove it permenantly from the todo list.",
            arguments=[
                Argument(
                    name="index",
                    description="the task index you wish to remove.",
                    usage_example="index=2",
                    help_content="the task index in the target list, it gets shown when first adding the task, or can be retrived via ``>todo list`` when the list is active.",
                    type=TypesRegistry.require(Types.INT),
                    required=True,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.remove_handler),
        )
        return {remove.name: remove}

    def get_remove_all(self) -> dict[str, Command]:
        remove_all = Command(
            name="removes",
            description="remove tasks in a range [start:end] from the current active list.",
            usage_example="todo remove index=2",
            help_content="by removing a task you remove it permenantly from the todo list.",
            arguments=[
                Argument(
                    name="start",
                    description="the start of the range (inclusive).",
                    usage_example="start=2",
                    type=TypesRegistry.require(Types.INT),
                    required=True,
                ),
                Argument(
                    name="end",
                    description="the end of the range (inclusive).",
                    usage_example="end=5",
                    type=TypesRegistry.require(Types.INT),
                    required=True,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.remove_all_handler),
        )
        return {remove_all.name: remove_all}

    def get_update(self) -> dict[str, Command]:
        update = Command(
            name="update",
            description="update a task from the current active list.",
            usage_example='>todo update index=2 task=go to the gym priority=high due="1d"',
            help_content=(
                "by updating a task you overwrite its old content with "
                "the new supplied content. task, priority and due are all "
                "optional now. supply only what changes (priority=none "
                "or due=none clears the field)."
            ),
            arguments=[
                Argument(
                    name="index",
                    description="the task index you wish to update.",
                    usage_example="index=2",
                    help_content="the task index in the target list, it gets shown when first adding the task, or can be retrived via ``>todo list`` when the list is active.",
                    type=TypesRegistry.require(Types.INT),
                    required=True,
                ),
                Argument(
                    name="task",
                    description="the updated task.",
                    usage_example="index=2",
                    help_content="this will overwrite the old task with the new one. (optional) leave it to only change priority/due.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
                Argument(
                    name="priority",
                    description="new priority: high, medium, low, or none to clear.",
                    usage_example="priority=high",
                    type=TypesRegistry.require(Types.WORD),
                    required=False,
                ),
                Argument(
                    name="due",
                    description="new due time, or none to clear.",
                    usage_example='due="tomorrow 9am"',
                    type=TypesRegistry.require(Types.WORD),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.update_handler),
        )
        return {update.name: update}

    def get_list(self) -> dict[str, Command]:
        ls = Command(
            name="list",
            description="list all tasks list for the active list or a specific list.",
            usage_example=">todo list",
            arguments=[
                Argument(
                    name="list",
                    description="the list name to list its tasks.",
                    usage_example="list=our work list",
                    help_content="defaults to the current active list if left blank.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
                Argument(
                    name="author",
                    description="the author of the list.",
                    usage_example="author=@opposingfork",
                    help_content="defaults to you if left blank",
                    type=TypesRegistry.require(Types.USER),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.list_handler),
        )
        return {ls.name: ls}

    def get_editros(self) -> dict[str, Command]:
        editors = Command(
            name="editors",
            description="list all editors for the active list or a specific list, with their permsissions.",
            usage_example=">todo list editors",
            arguments=[
                Argument(
                    name="list",
                    description="the list identifier to list its editors.",
                    usage_example="list=my work list",
                    help_content="defaults to the current active list if left blank.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.list_editors_handler),
        )
        return {editors.name: editors}

    def get_set_editor(self) -> dict[str, Command]:
        editor = Command(
            name="set-editor",
            description="set an editor for the current list with permsissions.",
            usage_example=">todo set-editor user=@opposingfork perms='rwd'",
            help_content="this command is also used to edit existing editor's permsissions.",
            arguments=[
                Argument(
                    name="user",
                    description="the user to set editor.",
                    usage_example="user=@opposingfork",
                    type=TypesRegistry.require(Types.USER),
                    required=True,
                ),
                Argument(
                    name="perms",
                    description="what this user is allowed to do to the list, defaults to read only if left omitted.",
                    help_content="permsissions are represented by string, avilable permsissions are 'r': read, 'w': write, 'd': delete, you can supply more than one permsission at the time like 'rdw'.",
                    usage_example="perms='rwd'",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
                Argument(
                    name="list",
                    description="the list identifier to set and editor for.",
                    usage_example="list=my work list",
                    help_content="defaults to the current active list if left blank.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.set_editor_handler),
        )
        return {editor.name: editor}

    def get_remove_editor(self) -> dict[str, Command]:
        editor = Command(
            name="remove-editor",
            description="remove an editor from the current list's editors.",
            usage_example=">todo remove-editor user=@opposingfork",
            arguments=[
                Argument(
                    name="user",
                    description="the user to set editor.",
                    usage_example="user=@opposingfork",
                    type=TypesRegistry.require(Types.USER),
                    required=True,
                ),
                Argument(
                    name="list",
                    description="the list identifier to remove and editor from.",
                    usage_example="list=my work list",
                    help_content="defaults to the current active list if left blank.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.remove_editor_handler),
        )
        return {editor.name: editor}

    def get_contributions(self) -> dict[str, Command]:
        contributions = Command(
            name="contributions",
            description="show all lists that you are an editor in with your permsissions for each one.",
            usage_example=">todo contributions",
            arguments=[],
            subcommands={},
            binding=NativeBinding(self.contributions_handler),
        )
        return {contributions.name: contributions}

    def get_create(self) -> dict[str, Command]:
        create = Command(
            name="create",
            description="create a new list",
            usage_example=">todo create name=my new list active=yes",
            arguments=[
                Argument(
                    name="name",
                    description="the list name to be created.",
                    usage_example="name=my awesome list",
                    help_content="the name of the list, it can be left blank then it will default to 'list#N', where in is first avilable number in ascending order.",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                ),
                Argument(
                    name="active",
                    description="automatically activates the list.",
                    usage_example="active=true",
                    help_content="on by default. by activating the list you make it the target for list commands like add/remove/update..",
                    type=TypesRegistry.require(Types.BOOL),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.create_handler),
        )
        return {create.name: create}

    def get_discard(self) -> dict[str, Command]:
        discard = Command(
            name="discard",
            description="discard a list",
            usage_example=">todo discard name=my obsolete list",
            help_content="by discarding a list your remove it permenantly from the todo lists.",
            arguments=[
                Argument(
                    name="identifier",
                    description="the list identifier to be discarded.",
                    help_content="identifier can either be the list name or its index.",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.discard_handler),
        )
        return {discard.name: discard}

    def get_lists(self) -> dict[str, Command]:
        lists = Command(
            name="lists",
            description="list all created lists names and their tasks count.",
            usage_example=">todo lists",
            arguments=[],
            subcommands={},
            binding=NativeBinding(self.lists_handler),
        )
        return {lists.name: lists}

    def get_activate(self) -> dict[str, Command]:
        activate = Command(
            name="activate",
            description="activate a list",
            usage_example=">todo activate name=terraria objectives",
            help_content="by activating a list you make it the target for list specific commands (add/remove/update/...).",
            arguments=[
                Argument(
                    name="identifier",
                    description="the list identifier that you wish to activate.",
                    usage_example="identifier=my list",
                    help_content="identifier can either be the list name or its index.",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                ),
                Argument(
                    name="author",
                    description="the author of the list that you wish to activate.",
                    usage_example="author=@opposingfork",
                    help_content="defaults to one of your lists if left blank.",
                    type=TypesRegistry.require(Types.USER),
                    required=False,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.activate_handler),
        )
        return {activate.name: activate}

    def get_deactivate(self) -> dict[str, Command]:
        activate = Command(
            name="deactivate",
            description="deactivate the active list",
            usage_example=">todo deactivate",
            arguments=[],
            subcommands={},
            binding=NativeBinding(self.deactivate_handler),
        )
        return {activate.name: activate}

    async def undo_handler(self, invocation: Invocation):
        undone = self.todo.undo(invocation.message.author.id)
        if undone is None:
            await invocation.message.reply(
                "nothing to undo - no recorded changes to your lists "
                "(history keeps the last 30).",
                mention_author=False,
            )
            return
        actor = undone.get("actor")
        when = undone.get("at")
        stamp = time.strftime("%H:%M", time.localtime(when)) if when else "?"
        who = f" by <@{actor}>" if actor else ""
        left = len(self.todo.log(invocation.message.author.id, limit=10**6))
        await invocation.message.reply(
            f"undid '{undone.get('action')}'{who} (at {stamp}). "
            f"{left} older change(s) still undoable.",
            mention_author=False,
        )

    async def log_handler(self, invocation: Invocation):
        limit = invocation.arguments.get("limit")
        limit = int(limit) if limit is not None else 10
        entries = self.todo.log(invocation.message.author.id, limit=limit)
        if not entries:
            await invocation.message.reply(
                "no recorded changes to your lists yet.", mention_author=False
            )
            return
        lines = []
        for e in entries:
            actor = e.get("actor")
            when = e.get("at")
            stamp = time.strftime("%m-%d %H:%M", time.localtime(when)) if when else "?"
            who = f"<@{actor}>" if actor else "?"
            lines.append(f"{stamp} · {e.get('action')} by {who}")
        await paginator.paginate(
            invocation.message,
            "changes to your lists (newest first):\n" + "\n".join(lines),
            client=self.context.client,
        )

    async def backup_handler(self, invocation: Invocation):
        name = self.todo.backup()
        total = len(self.todo.backups())
        await invocation.message.reply(
            f"backed up everyone's todo data to `{name}` "
            f"({total} kept, oldest rotate out at 12).",
            mention_author=False,
        )

    async def restore_handler(self, invocation: Invocation):
        ident = invocation.arguments.get("id")
        names = self.todo.backups()
        if ident is None:
            if not names:
                await invocation.message.reply(
                    "no backups yet - `>todo backup` makes one, and one "
                    "lands automatically every 25 mutations.",
                    mention_author=False,
                )
                return
            lines = [f"{i}: `{name}`" for i, name in enumerate(names, start=1)]
            await invocation.message.reply(
                "backups (newest last - load with `>todo restore id=N`):\n"
                + "\n".join(lines),
                mention_author=False,
            )
            return
        if not 1 <= int(ident) <= len(names):
            await invocation.message.reply(
                f"id must be 1..{len(names)} - see `>todo restore`.",
                mention_author=False,
            )
            return
        count = self.todo.restore_backup(names[int(ident) - 1])
        await invocation.message.reply(
            f"restored `{names[int(ident) - 1]}` - {count} user(s) of todo "
            "data are back to that moment.",
            mention_author=False,
        )

    def _parse_priority(self, value) -> str | None:
        raw = str(value or "").strip().strip('"').strip("'").strip().lower()
        if not raw or raw in PRIORITY_CLEAR:
            return None
        if raw not in PRIORITY_ALIASES:
            raise CogError(
                f"priority must be high, medium or low (h, m, l) (got '{value}'). ",
                is_user_error=True,
            )
        return PRIORITY_ALIASES[raw]

    def _parse_due(self, value, tz=None) -> float | None:
        raw = str(value or "").strip().strip('"').strip("'").strip()
        if not raw or due_mod.is_clear_token(raw):
            return None
        try:
            return due_mod.parse_due(raw, tz=tz).epoch
        except due_mod.DueError as e:
            raise CogError(str(e), is_user_error=True)

    def _tz_of(self, user: int):
        """the user's stored tz name, or None (falls back to LEECH_TZ)."""
        return self.todo.tz_of(user)

    def _render_task_lines(self, todolist, *, tz=None) -> list[str]:
        lines = []
        for i, task in self.todo.sorted_tasks(todolist):
            emote = PRIORITY_EMOTE.get(task.priority or "")
            lead = f"{emote} " if emote else ""
            tail = []
            if task.due is not None:
                # v10.9: due rides after the task like the priority emote
                # rides before it - no parentheses wrapper.
                tail.append(due_mod.format_due(task.due, tz=tz))
                if task.reminded:
                    tail.append("🔔 pinged")
            suffix = f" {' · '.join(tail)}" if tail else ""
            lines.append(f"{lead}{i}: {task.content}{suffix}")
        return lines

    def _origin_of(self, invocation: Invocation) -> TaskOrigin:
        message = invocation.message
        return TaskOrigin(
            channel_id=message.channel.id,
            guild_id=getattr(message.guild, "id", None),
            user_id=message.author.id,
        )

    async def add_handler(self, invocation: Invocation):
        task = invocation.arguments["task"]
        user = invocation.message.author.id
        tz = self._tz_of(user)
        priority = self._parse_priority(invocation.arguments.get("priority"))
        due = self._parse_due(invocation.arguments.get("due"), tz=tz)
        origin = self._origin_of(invocation) if due is not None else None
        index = self.todo.add_task(
            user,
            task,
            priority=priority,
            due=due,
            origin=origin,
        )
        notes = []
        if priority:
            notes.append(f"priority {PRIORITY_EMOTE[priority]} {priority}")
        if due is not None:
            notes.append(
                f"due {due_mod.render(due, tz=tz)} (in {due_mod.humanize(due - time.time())})"
            )
        suffix = f" ({', '.join(notes)})" if notes else ""
        await invocation.message.reply(f"added task: at index: {index}.{suffix}")

    async def adds_handler(self, invocation: Invocation):
        tasks_raw = str(invocation.arguments["tasks"])
        tasks = tasks_raw.split("\\")
        user = invocation.message.author.id
        tz = self._tz_of(user)
        priority = self._parse_priority(invocation.arguments.get("priority"))
        due = self._parse_due(invocation.arguments.get("due"), tz=tz)
        origin = self._origin_of(invocation) if due is not None else None
        indices = []
        for task in tasks:
            indices.append(
                self.todo.add_task(
                    user,
                    task,
                    priority=priority,
                    due=due,
                    origin=origin,
                )
            )
        suffix = (
            f" (priority {priority}, due {due_mod.render(due, tz=tz)})"
            if priority and due
            else ""
        )
        await invocation.message.reply(
            f"added tasks: at indices: {indices[0]}...{indices[-1]}.{suffix}"
        )

    async def remove_handler(self, invocation: Invocation):
        index = invocation.arguments["index"]
        task = self.todo.remove_task(invocation.message.author.id, index)
        await invocation.message.reply(f"removed task at: {index}.")

    async def remove_all_handler(self, invocation: Invocation):
        start = invocation.arguments["start"]
        end = invocation.arguments["end"]
        task = self.todo.remove_tasks(invocation.message.author.id, start, end)
        await invocation.message.reply(f"removed tasks {f'at {start}' if start == end else f'from {start}:{end}'}.")

    async def update_handler(self, invocation: Invocation):
        index = invocation.arguments["index"]
        task_arg = invocation.arguments.get("task")
        priority_arg = invocation.arguments.get("priority")
        due_arg = invocation.arguments.get("due")
        if task_arg is None and priority_arg is None and due_arg is None:
            raise CogError(
                "nothing to update: give task, priority or due.",
                is_user_error=True,
            )
        tz = self._tz_of(invocation.message.author.id)
        old = self.todo.update_task(
            invocation.message.author.id,
            index,
            task_arg if task_arg is not None else UNSET,
            priority=(
                self._parse_priority(priority_arg)
                if priority_arg is not None
                else UNSET
            ),
            due=self._parse_due(due_arg, tz=tz) if due_arg is not None else UNSET,
        )
        await invocation.message.reply(
            f"updated task at: {index} from: '{task_preview(old)}'."
        )

    async def list_handler(self, invocation: Invocation):
        author = invocation.arguments.get("author")
        lst = invocation.arguments.get("list")
        lst = lst if lst is not None else ""
        author = invocation.message.author.id if author is None else author
        name, todolist = self.todo.list_tasks(invocation.message.author.id, author, lst)
        tz = self._tz_of(invocation.message.author.id)
        lines = [f"list '{name}' ({len(todolist.todo)} tasks):"]
        lines += self._render_task_lines(todolist, tz=tz)
        text = "\n".join(lines)
        invocation.chain.out.append(text)
        await paginator.paginate(invocation.message, text, client=self.context.client)

    def get_tz(self) -> dict[str, Command]:
        tz = Command(
            name="tz",
            description="set your timezone - due times parse and render in it (fallback: the bot's LEECH_TZ).",
            usage_example=">todo tz set=Asia/Damascus",
            help_content=(
                "times like `tomorrow 9am` or `15:00` are read in YOUR "
                "timezone once set, and dues you see are rendered in it "
                "too. unset (set=none) falls back to the bot's LEECH_TZ. "
                "reminders and todos both use it."
            ),
            arguments=[
                Argument(
                    name="set",
                    description="a timezone name like Asia/Damascus (omit to just see your current one).",
                    usage_example="set=Europe/Berlin",
                    type=TypesRegistry.require(Types.STR),
                    required=False,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.tz_handler),
        )
        return {tz.name: tz}

    def get_exclude(self) -> dict[str, Command]:
        exclude = Command(
            name="exclude",
            description="keep a list out of the `>todo lists` overall task counter.",
            usage_example=">todo exclude name=hoard",
            help_content=(
                "for hoarding lists you don't want messing with the "
                "counter: excluded lists still show in `>todo lists` (with "
                "an `(excluded)` mark) but their tasks stop counting "
                "towards the total. the list itself works exactly as "
                "before. undo with `>todo include`."
            ),
            arguments=[
                Argument(
                    name="identifier",
                    description="the list identifier to exclude (name or index).",
                    usage_example="identifier=hoard",
                    help_content="identifier can either be the list name or its index.",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.exclude_handler),
        )
        return {exclude.name: exclude}

    def get_include(self) -> dict[str, Command]:
        include = Command(
            name="include",
            description="count a list's tasks in the `>todo lists` counter again.",
            usage_example=">todo include name=hoard",
            help_content="undoes `>todo exclude` - the list's tasks count towards the total again.",
            arguments=[
                Argument(
                    name="identifier",
                    description="the list identifier to include (name or index).",
                    usage_example="identifier=hoard",
                    help_content="identifier can either be the list name or its index.",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.include_handler),
        )
        return {include.name: include}

    def get_remind_subcommands(self) -> dict[str, Command]:
        add = Command(
            name="add",
            description="add a reminder that pings you when the time is up.",
            usage_example='>remind add call mom due="tomorrow 9am"',
            help_content=(
                "due accepts 30m, 2h, 1d6h, 15:00, 3:30pm, today 15:00, "
                "tonight, tomorrow 9am, friday 3pm, 2026-10-24 15:00... "
                "read in your `>todo tz` timezone (else the bot's)."
            ),
            arguments=[
                Argument(
                    name="task",
                    description="what to remind you about.",
                    usage_example="task=call mom",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                ),
                Argument(
                    name="due",
                    description="when to ping you (30m, 15:00, tomorrow 9am, 10-24 ...).",
                    usage_example='due="tomorrow 9am"',
                    help_content='multi-word times need quotes: due="tomorrow 9am".',
                    type=TypesRegistry.require(Types.WORD),
                    required=True,
                ),
            ],
            subcommands={},
            binding=NativeBinding(self.remind_add_handler),
        )
        ls = Command(
            name="list",
            description="show pending reminders.",
            usage_example=">remind list",
            arguments=[],
            subcommands={},
            binding=NativeBinding(self.remind_list_handler),
        )
        return {add.name: add, ls.name: ls}

    async def tz_handler(self, invocation: Invocation):
        user = invocation.message.author.id
        value = invocation.arguments.get("set")
        raw = str(value or "").strip().strip('"').strip("'").strip()
        current = self.todo.tz_of(user)
        if value is None:
            if current:
                body = (
                    f"your timezone: {current} "
                    f"(it is {due_mod.render(time.time(), tz=current)} there)."
                )
            else:
                body = (
                    "your timezone: (unset) - times fall back to the bot's "
                    f"LEECH_TZ ({due_mod.timezone().key})."
                )
            body += "\nset it with `>todo tz set=Asia/Damascus` (set=none clears)."
            await invocation.message.reply(body, mention_author=False)
            return
        if not raw or due_mod.is_clear_token(raw):
            self.todo.set_tz(user, None)
            await invocation.message.reply(
                "cleared - times fall back to the bot's LEECH_TZ.",
                mention_author=False,
            )
            return
        try:
            zone = due_mod.valid_tz(raw)
        except ValueError as e:
            raise CogError(str(e), is_user_error=True)
        self.todo.set_tz(user, zone.key)
        await invocation.message.reply(
            f"your timezone is now {zone.key} 🌍 "
            f"(it is {due_mod.render(time.time(), tz=zone.key)} there).",
            mention_author=False,
        )

    async def exclude_handler(self, invocation: Invocation):
        name = self.todo.set_excluded(
            invocation.message.author.id,
            invocation.arguments["identifier"],
            True,
        )
        await invocation.message.reply(
            f"list '{name}' is out of the `>todo lists` counter "
            "(`>todo include` brings it back).",
            mention_author=False,
        )

    async def include_handler(self, invocation: Invocation):
        name = self.todo.set_excluded(
            invocation.message.author.id,
            invocation.arguments["identifier"],
            False,
        )
        await invocation.message.reply(
            f"list '{name}' counts towards the `>todo lists` total again.",
            mention_author=False,
        )

    async def remind_add_handler(self, invocation: Invocation):
        user = invocation.message.author.id
        tz = self._tz_of(user)
        task = invocation.arguments["task"]
        due = self._parse_due(invocation.arguments["due"], tz=tz)
        if due is None:
            raise CogError(
                "a reminder needs a real due time (like due=30m or "
                'due="tomorrow 9am").',
                is_user_error=True,
            )
        index = self.todo.add_reminder(user, task, due, self._origin_of(invocation))
        await invocation.message.reply(
            f"reminder {index} set: '{task_preview(task)}' "
            f"{due_mod.render(due, tz=tz)} "
            f"(in {due_mod.humanize(due - time.time())}).",
        )

    async def remind_list_handler(self, invocation: Invocation):
        user = invocation.message.author.id
        try:
            name, todolist = self.todo.list_tasks(user, user, "reminders")
        except BaseError:
            await invocation.message.reply(
                "no reminders yet - set one with "
                '`>remind add call mom due="tomorrow 9am"`.',
                mention_author=False,
            )
            return
        lines = [f"reminders ({len(todolist.todo)} pending):"]
        lines += self._render_task_lines(todolist, tz=self._tz_of(user))
        text = "\n".join(lines)
        invocation.chain.out.append(text)
        await paginator.paginate(invocation.message, text, client=self.context.client)

    async def list_editors_handler(self, invocation: Invocation):
        lst = invocation.arguments.get("list")
        lst = lst if lst is not None else ""
        name, todolist = self.todo.list_tasks(
            invocation.message.author.id, invocation.message.author.id, lst
        )
        rawtext = "\n".join(
            f"<@{uid}>: perms: '{format_perms(perms)}'"
            for uid, perms in todolist.editors.items()
        )
        if not todolist.editors:
            rawtext = "no editors."
        msg = f"**{name}**\n{rawtext}"
        await paginator.paginate(invocation.message, msg, client=self.context.client)

    async def set_editor_handler(self, invocation: Invocation):
        editor = invocation.arguments["user"]
        perms = invocation.arguments.get("perms")
        perms = parse_perms(perms)
        lst = invocation.arguments.get("list")
        lst = lst if lst is not None else ""
        self.todo.set_editor(
            user=invocation.message.author.id,
            editor=editor,
            perms=perms,
            list_ident=lst,
        )
        await invocation.message.reply(
            f"user: <@{editor}> is now an editor with perms: {format_perms(perms)}"
        )

    async def remove_editor_handler(self, invocation: Invocation):
        editor = invocation.arguments["user"]
        lst = invocation.arguments.get("list")
        lst = lst if lst is not None else ""
        self.todo.remove_editor(
            user=invocation.message.author.id, editor=editor, list_ident=lst
        )
        await invocation.message.reply(f"user: <@{editor}> is no longer an editor.")

    async def create_handler(self, invocation: Invocation):
        name = invocation.arguments.get("name")
        active = invocation.arguments.get("active")

        name = name if name is not None else ""
        active = active if active is not None else True

        name, i = self.todo.create_list(invocation.message.author.id, active, name)
        await invocation.message.reply(
            f"created list: '{name}'{', and activated it' if active else ''}."
        )

    async def discard_handler(self, invocation: Invocation):
        name = invocation.arguments["identifier"]
        self.todo.discard_list(invocation.message.author.id, name)
        await invocation.message.reply(f"discarded list: `{name}`.")

    async def lists_handler(self, invocation: Invocation):
        lists = self.todo.list_lists(invocation.message.author.id)
        total = 0
        excluded_tasks = 0

        rows = []
        for i, (name, lst) in enumerate(lists.items()):
            count = len(lst.todo)
            if lst.excluded:
                excluded_tasks += count
                rows.append(f"**{i}**: '{name}', tasks count: {count} (excluded)")
            else:
                total += count
                rows.append(f"**{i}**: '{name}', tasks count: {count}")
        rawtext = "\n".join(rows)
        footer = f"\n> total: {total} tasks"
        if excluded_tasks:
            footer += f" (+{excluded_tasks} in excluded lists). (sum {excluded_tasks + total})."
        else:
            footer += "."
        rawtext += footer
        if not lists:
            rawtext = "no lists."
        await paginator.paginate(
            invocation.message, rawtext, client=self.context.client
        )

    async def contributions_handler(self, invocation: Invocation):
        contributions = self.todo.list_contributions(invocation.message.author.id)
        total = 0
        rawtext = "\n".join(
            f"'{handle.handle.name}' <@{handle.handle.author_id}> with perms: '{format_perms(handle.perms)}'"
            for handle in contributions
        )

        rawtext += f"\n> total: {len(contributions)} contributions"
        if not contributions:
            rawtext = "no contributions."
        await paginator.paginate(
            invocation.message, rawtext, client=self.context.client
        )

    async def activate_handler(self, invocation: Invocation):
        name = invocation.arguments["identifier"]
        author = invocation.arguments.get("author")
        author = author if author is not None else invocation.message.author.id
        self.todo.activate_list(invocation.message.author.id, name, author)
        await invocation.message.reply(f"activated list: '{name}'.")

    async def deactivate_handler(self, invocation: Invocation):
        self.todo.deactivate_list(invocation.message.author.id)
        await invocation.message.reply(f"deactivated the list.")

    async def _due_loop(self) -> None:
        while True:
            await asyncio.sleep(REMINDER_SCAN)
            try:
                for spec in self.todo.due_ready():
                    self.context.spawn(self._fire_reminder(spec), name="todo-due-ping")
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("todo due scan failed")

    async def _fire_reminder(self, spec: dict) -> None:
        task = spec["task"]
        origin = task.origin
        if origin is None:
            return
        client = self.context.client
        preview = task_preview(task.content)

        body = (
            f"⏰ task due: **{preview}**\n"
            "reply `snooze 30m` (or a new time like `15:00`, "
            "`tomorrow 9am`) to delay it, `done` to finish it (removes "
            "it), or `ignore` to keep it as is. or just ignore me."
        )

        target = None
        channel = client.get_channel(origin.channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(origin.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if channel is not None:
            try:
                target = await channel.send(
                    f"<@{origin.user_id}> {body}",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                target = None

        if target is None:
            try:
                user = client.get_user(origin.user_id)
                if user is None:
                    user = await client.fetch_user(origin.user_id)
                target = await user.create_dm().send(f"{body}")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                _logger.warning(
                    f"todo due ping gave up (no channel, no dm) for user "
                    f"{origin.user_id}: {e}"
                )
                return
        try:
            await self._await_reminder_answer(
                target,
                pinged=origin.user_id,
                owner=spec["owner"],
                list_name=spec["list"],
                task_id=spec["task_id"],
                preview=preview,
                tz=self._tz_of(origin.user_id),
            )
        except Exception:
            _logger.exception("todo reminder prompt failed")

    async def _await_reminder_answer(
        self,
        message: discord.Message,
        *,
        pinged: int,
        owner: int,
        list_name: str,
        task_id: int | None,
        preview: str,
        tz=None,
    ) -> None:
        if task_id is None:
            return
        session = ReplySession(
            self.context.client,
            message.channel.id,
            pinged,
            regex=REMINDER_REPLY_RE,
            target_id=message.id,
            name="todo-due",
        )
        try:
            deadline = time.monotonic() + REMINDER_PROMPT_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await _say(message, "no answer: keeping the task as-is.")
                    return
                reply = await session.next(timeout=remaining)
                if reply is None:
                    await _say(message, "no answer: keeping the task as-is.")
                    return
                handled = await self._apply_reminder_answer(
                    reply,
                    message=message,
                    owner=owner,
                    list_name=list_name,
                    task_id=task_id,
                    preview=preview,
                    tz=tz,
                )
                if handled:
                    return
        finally:
            session.close()

    async def _apply_reminder_answer(
        self,
        reply: discord.Message,
        *,
        message: discord.Message,
        owner: int,
        list_name: str,
        task_id: int,
        preview: str,
        tz=None,
    ) -> bool:
        actor = reply.author.id
        text = (reply.content or "").strip().lower()
        word = text.split(" ", 1)[0] if text else ""

        if word in IGNORE_WORDS:
            await _say(message, "ok, keeping it.")
            return True

        if word in DONE_WORDS:
            try:
                removed = self.todo.finish_task(owner, list_name, task_id, actor=actor)
                await _say(message, f"done - removed '{removed}'.")
            except BaseError as e:
                await _say(message, f"couldn't remove it: {e.message}")
            return True

        try:
            when = due_mod.parse_snooze(text, tz=tz)
            self.todo.reschedule_task(owner, list_name, task_id, when, actor=actor)
            await _say(
                message,
                f"pushed '{preview}' to {due_mod.render(when, tz=tz)} "
                f"(in {due_mod.humanize(when - time.time())}).",
            )
            return True
        except due_mod.DueError as e:
            await _say(
                message,
                f"not a proper time: try `snooze 30m`, a time like "
                f"`15:00` / `tomorrow 9am`, or `done`. ({e})",
            )
            return False
        except BaseError as e:
            await _say(message, f"couldn't delay it: {e.message}")
            return True


async def _say(message: discord.Message, text: str) -> None:
    try:
        await message.reply(text, mention_author=False)
    except discord.HTTPException:
        pass


def create_cog(context: CogContext):
    return TodoCog(context=context)
