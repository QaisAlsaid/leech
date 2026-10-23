from leech.core.cog.cog import Cog, CogContext
from leech.core.cog.error import CogError
from leech.core.command.runtime import Command, Argument
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.command.native.binding import NativeBinding
from leech.core.command.mock.binding import MockBinding
from leech.core.error import BaseError
import leech.core.helpers.text.paginator as paginator
import time
from .service import Perms, TodoService, UNDO_KEEP

DISCORD_MESSAGE_LIMIT = 2000

TASK_PREVIEW = 25


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
            **self.get_undo(),
            **self.get_log(),
            **self.get_backup(),
            **self.get_restore(),
        }

    def get_undo(self) -> dict[str, Command]:
        undo = Command(
            name="undo",
            description="undo the last change to YOUR lists (whoever made it).",
            usage_example=">todo undo",
            help_content=(
                "restores your lists to how they were before the most "
                "recent mutation - even if the AI or another editor made "
                "it. undo again to walk back another step (the last "
                f"{UNDO_KEEP} steps are kept). once you make a new edit, the "
                "undone path is gone - there is no redo."
            ),
            arguments=[],
            subcommands={},
            binding=NativeBinding(self.undo_handler),
        )
        return {undo.name: undo}

    def get_log(self) -> dict[str, Command]:
        log = Command(
            name="log",
            description="who changed your lists, and when.",
            usage_example=">todo log",
            help_content=(
                "the last mutations on YOUR lists (by you, by editors, or "
                "by the AI acting through the dispatcher) - newest first. "
                "this is the audit trail that was missing the night the "
                "AI removed tasks from the wrong list."
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
                "(the newest 12 are kept; one also lands automatically "
                "every 25 mutations). restore them with `>todo restore`."
            ),
            arguments=[],
            subcommands={},
            binding=NativeBinding(self.backup_handler),
        )
        return {backup.name: backup}

    def get_restore(self) -> dict[str, Command]:
        restore = Command(
            name="restore",
            description="list backups, or load one (owner only).",
            usage_example=">todo restore id=1",
            help_content=(
                "without id: lists the saved backups, newest last. with "
                "id: replaces ALL users' todo data with that backup "
                "(that's why it's owner-only). the replace itself is "
                "undo-protected - it snapshots first... for every user, "
                "so be decisive."
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
            usage_example=">todo add task=finish writing forkic novel!",
            arguments=[
                Argument(
                    name="task",
                    description="the task you wish to add.",
                    usage_example="task=finish writing forkic novel.",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                )
            ],
            subcommands={},
            binding=NativeBinding(self.add_handler),
        )
        return {add.name: add}

    def get_add_all(self) -> dict[str, Command]:
        add_all = Command(
            name="adds",
            description="add '\\' separated tasks to the current active list.",
            usage_example=">todo adds tasks=finish writing forkic novel!\\draw daily bird",
            arguments=[
                Argument(
                    name="tasks",
                    description="the tasks you wish to add ('\\' separated).",
                    usage_example="tasks=finish writing forkic novel.\\draw daily bird",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
                )
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

    def get_update(self) -> dict[str, Command]:
        update = Command(
            name="update",
            description="update a task from the current active list.",
            usage_example=">todo update index=2 taks=go to the gym.",
            help_content="by updating a task you overwrite its old content with the new supplied content.",
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
                    help_content="this will overwrite the old task with the new one.",
                    type=TypesRegistry.require(Types.STR),
                    required=True,
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
                    usage_example="list=my work list",
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
            lines = [
                f"{i}: `{name}`" for i, name in enumerate(names, start=1)
            ]
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

    async def add_handler(self, invocation: Invocation):
        task = invocation.arguments["task"]
        index = self.todo.add_task(invocation.message.author.id, task)
        await invocation.message.reply(f"added task: at index: {index}.")

    async def adds_handler(self, invocation: Invocation):
        tasks_raw = str(invocation.arguments["tasks"])
        tasks = tasks_raw.split("\\")
        indices = []
        for task in tasks:
            indices.append(self.todo.add_task(invocation.message.author.id, task))
        await invocation.message.reply(f"added tasks: at indices: {indices[0]}...{indices[-1]}.")

    async def remove_handler(self, invocation: Invocation):
        index = invocation.arguments["index"]
        task = self.todo.remove_task(invocation.message.author.id, index)
        await invocation.message.reply(f"removed task at: {index}.")

    async def update_handler(self, invocation: Invocation):
        index = invocation.arguments["index"]
        task = invocation.arguments["task"]
        old = self.todo.update_task(invocation.message.author.id, index, task)
        await invocation.message.reply(
            f"updated task at: {index} from: '{task_preview(old)}' to: '{task_preview(task)}'."
        )

    async def list_handler(self, invocation: Invocation):
        author = invocation.arguments.get("author")
        lst = invocation.arguments.get("list")
        lst = lst if lst is not None else ""
        author = invocation.message.author.id if author is None else author
        name, todolist = self.todo.list_tasks(invocation.message.author.id, author, lst)
        lines = [f"list '{name}' ({len(todolist.todo)} tasks):"] + [
            f"{i}: {task.content}" for i, task in enumerate(todolist.todo)
        ]
        invocation.chain.out.append("\n".join(lines))
        await paginator.paginate(
            invocation.message, "\n".join(lines), client=self.context.client
        )

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
        await paginator.paginate(
            invocation.message, msg, client=self.context.client
        )

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

        def accum(i):
            nonlocal total
            total += i
            return i

        rawtext = "\n".join(
            f"**{i}**: '{name}', tasks count: {accum(len(lst.todo))}"
            for i, (name, lst) in enumerate(lists.items())
        )
        rawtext += f"\n> total: {total} tasks."
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


def create_cog(context: CogContext):
    return TodoCog(context=context)
