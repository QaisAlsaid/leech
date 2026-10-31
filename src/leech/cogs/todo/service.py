from pydantic import BaseModel, Field, TypeAdapter, field_validator
from leech.core.error import BaseError
from leech.core.store import Store, get_store
import re
import time

UNSET = object()

PRIORITIES = ("high", "medium", "low")
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}


def _check_priority(priority) -> str | None:
    if priority is None:
        return None
    text = str(priority).strip().strip('"').strip("'").strip().lower()
    if text in ("", "none", "null"):
        return None
    if text not in PRIORITIES:
        raise TodoError(f"priority must be high, medium or low (got '{priority}').")
    return text


def _check_due(due) -> float | None:
    if due is None:
        return None
    if isinstance(due, bool) or not isinstance(due, (int, float)):
        raise TodoError("a due time must be a unix timestamp in seconds.")
    return float(due)


class TaskOrigin(BaseModel):
    channel_id: int
    guild_id: int | None = None
    user_id: int  # who to ping (the adder)


class TodoTask(BaseModel):
    content: str
    author_id: int | None = None
    id: int | None = None  # creation order identity (ms stamp)
    priority: str | None = None  # high | medium | low | None
    due: float | None = None  # unix seconds
    reminded: bool = False  # ping fired (and was answered or given up)
    origin: TaskOrigin | None = None
    # v12: stable identity + bookkeeping. `number` is the per-list task
    # number (1-based, never reused), `branch` is the tree suffix ("a",
    # "b"...), `tree` marks a tree root. legacy rows get numbers
    # reconciled from their position on load.
    number: int | None = None
    branch: str = ""
    tree: bool = False
    created_at: float | None = None  # unix seconds
    edited_at: float | None = None  # unix seconds
    edited_count: int = 0

    @field_validator("priority", mode="before")
    @classmethod
    def _blank_priority(cls, value):
        return None if value in ("", "none", "null") else value


class Perms(BaseModel):
    read: bool = True
    write: bool = False
    remove: bool = False


class TodoList(BaseModel):
    todo: list[TodoTask] = Field(default_factory=list)
    editors: dict[int, Perms] = Field(default_factory=dict)
    excluded: bool = False  # v10.9: hoard lists stay out of the `>todo lists` counter
    # v12: `seq` hands out task numbers (kept past everything seen so
    # numbers are never reused). `tree_root` is the task number of the
    # armed tree - while it is set, adds grow as branches (46a, 46b...)
    # of that root instead of taking fresh numbers.
    seq: int = 0
    tree_root: int | None = None

    def model_post_init(self, __context) -> None:
        reconcile_task_numbers(self)


class TodoListHandle(BaseModel):
    author_id: int
    name: str


class EditorHandle(BaseModel):
    handle: TodoListHandle
    perms: Perms


class TodoAuthor(BaseModel):
    author_id: int
    editor_in: list[EditorHandle] = Field(default_factory=list)


class TodoContainer(BaseModel):
    author: TodoAuthor
    active: TodoListHandle | None = None
    todo: dict[str, TodoList] = Field(default_factory=dict)
    # v10.9: postgres jsonb reorders object keys (shortest first, then
    # bytewise), so the dict above comes back shuffled after every
    # supabase round-trip. arrays survive jsonb intact - `list_order` is
    # the durable source of truth for list order. `>todo list 2`-style
    # numeric indices depend on it too.
    list_order: list[str] = Field(default_factory=list)

    @field_validator("author", mode="before")
    @classmethod
    def migrate_old_author(cls, author):
        if isinstance(author, int):
            return TodoAuthor(author_id=author)
        return author

    @field_validator("active", mode="before")
    @classmethod
    def migrate_old_active(cls, active):
        if isinstance(active, str):
            return None
        return active

    def model_post_init(self, __context) -> None:
        reconcile_order(self)


container_adapter = TypeAdapter(dict[int, TodoContainer])
MAX_LIST_NAME = 100
MAX_TASKS = 1000
MAX_LISTS = 100

REMINDERS_LIST = "reminders"


def reconcile_order(container: TodoContainer) -> None:
    """keep `list_order` and the `todo` dict agreeing, in that direction:
    remembered order first (minus deleted lists), never-seen lists
    appended in dict order, then the dict itself rebuilt in that order."""
    order = [name for name in container.list_order if name in container.todo]
    order += [name for name in container.todo if name not in order]
    container.list_order = order
    container.todo = {name: container.todo[name] for name in order}


TASK_REF_RE = re.compile(r"^(\d+)([a-z]*)$")


def reconcile_task_numbers(lst: TodoList) -> None:
    """v12: stable per-list task numbers. legacy rows (no `number`) get
    position+1 on load; numbered rows keep theirs; `seq` is pushed past
    everything seen so numbers are never reused. creation stamps are
    backfilled from the task id (a ms epoch) where plausible."""
    used = {t.number for t in lst.todo if t.number is not None}
    free = 1
    for task in lst.todo:
        if task.number is None:
            while free in used:
                free += 1
            task.number = free
            used.add(free)
        if task.created_at is None and task.id is not None and task.id > 10**12:
            task.created_at = round(task.id / 1000.0, 3)
    top = max(used, default=0)
    if lst.seq <= top:
        lst.seq = top + 1


def _branch_next(branch: str) -> str:
    """bijective base-26: a..z then aa, ab... ("" - the no-branch state
    - succeeds to "a")."""
    letters = list(branch)
    if not letters:
        return "a"
    for i in range(len(letters) - 1, -1, -1):
        if letters[i] != "z":
            letters[i] = chr(ord(letters[i]) + 1)
            return "".join(letters)
        letters[i] = "a"
    return "a" * (len(letters) + 1)


def _branch_last(lst: TodoList, root: int) -> str:
    branches = [t.branch for t in lst.todo if t.number == root and t.branch]
    return max(branches, key=lambda b: (len(b), b)) if branches else ""


def task_label(task: TodoTask) -> str:
    return f"{task.number}{task.branch}" if task.number is not None else "?"


UNDO_KEEP = 30
BACKUP_EVERY = 25
BACKUP_KEEP = 12

NS_TODO = "todo"
NS_UNDO = "todo_undo"
NS_BACKUP = "todo_backup"
NS_PREFS = "user_prefs"


class TodoError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


class TodoService:
    def __init__(self, store: Store | None = None):
        self.store = store or get_store()
        self.containers: dict[int, TodoContainer] = {
            int(key): (
                value
                if isinstance(value, TodoContainer)
                else TodoContainer.model_validate(value)
            )
            for key, value in self.store.items(NS_TODO).items()
        }
        self._mutations = 0

    def _snapshot(self, actor: int, action: str, *owners: int) -> None:
        for owner in dict.fromkeys(o for o in owners if o):
            entry = {
                "at": round(time.time(), 3),
                "actor": actor,
                "action": action,
                "state": (
                    self.containers[owner].model_dump(mode="json")
                    if owner in self.containers
                    else None
                ),
            }
            seq = int(time.time() * 1000)
            key = f"{owner}/{seq}"
            while self.store.get(NS_UNDO, key) is not None:
                seq += 1
                key = f"{owner}/{seq}"
            self.store.put(NS_UNDO, key, entry)
            entries = self._undo_entries(owner)
            for stale in entries[:-UNDO_KEEP]:
                self.store.delete(NS_UNDO, stale)
        self._mutations += 1
        if self._mutations % BACKUP_EVERY == 0:
            try:
                self.backup()
            except TodoError:
                pass

    def _undo_entries(self, user: int) -> list[str]:
        prefix = f"{user}/"
        return sorted(
            key for key in self.store.items(NS_UNDO) if key.startswith(prefix)
        )

    def undo(self, user: int) -> dict | None:
        entries = self._undo_entries(user)
        if not entries:
            return None
        key = entries[-1]
        entry = self.store.get(NS_UNDO, key) or {}
        state = entry.get("state")
        if state is None:
            self.containers.pop(user, None)
            self.store.delete(NS_TODO, str(user))
        else:
            self.containers[user] = TodoContainer.model_validate(state)
            self._persist(user)
        self.store.delete(NS_UNDO, key)
        return {
            "action": entry.get("action") or "?",
            "actor": entry.get("actor"),
            "at": entry.get("at"),
        }

    def log(self, user: int, limit: int = 10) -> list[dict]:
        out: list[dict] = []
        for key in reversed(self._undo_entries(user)[-max(1, limit) :]):
            entry = self.store.get(NS_UNDO, key) or {}
            out.append(
                {
                    "action": entry.get("action") or "?",
                    "actor": entry.get("actor"),
                    "at": entry.get("at"),
                }
            )
        return out

    def backup(self) -> str:
        name = time.strftime("%Y%m%d-%H%M%S")
        for container in self.containers.values():
            reconcile_order(container)
        self.store.put(
            NS_BACKUP,
            name,
            {"containers": container_adapter.dump_python(self.containers, mode="json")},
        )
        for stale in sorted(self.store.items(NS_BACKUP))[:-BACKUP_KEEP]:
            self.store.delete(NS_BACKUP, stale)
        return name

    def backups(self) -> list[str]:
        return sorted(self.store.items(NS_BACKUP))

    def restore_backup(self, name: str) -> int:
        if not re.fullmatch(r"\d{8}-\d{6}", name or ""):
            raise TodoError(
                "backup name must look like 20261022-025059. pick one "
                "from `>todo restore`."
            )
        data = self.store.get(NS_BACKUP, name)
        if not isinstance(data, dict) or "containers" not in data:
            raise TodoError(f"no backup named {name}. check `>todo restore`. ")
        try:
            containers = container_adapter.validate_python(data["containers"])
        except ValueError as e:
            raise TodoError(f"backup {name} is unreadable: {e}")
        self.containers = containers
        live = set(self.store.items(NS_TODO))
        wanted = {str(owner) for owner in containers}
        for stale in live - wanted:
            self.store.delete(NS_TODO, stale)
        for owner in containers:
            self._persist(owner)
        return len(containers)

    def _persist(self, owner: int) -> None:
        try:
            container = self.containers[owner]
            reconcile_order(container)
            self.store.put(
                NS_TODO,
                str(owner),
                container.model_dump(mode="json"),
            )
        except Exception as e:
            raise TodoError(
                f"failed to persist data. your edit will not be saved. details: {e}"
            ) from e

    def ensure_container(self, user: int) -> TodoContainer:
        container = self.containers.get(user)
        if container is None:
            container = TodoContainer(author=TodoAuthor(author_id=user))
            self.containers[user] = container

        return container

    def authorize(self, intents: Perms, perms: Perms) -> bool:
        if intents.read and not perms.read:
            return False
        if intents.write and not perms.write:
            return False
        if intents.remove and not perms.remove:
            return False
        return True

    def ensure_list(
        self, user: int, author: int, list_ident: str, intents: Perms
    ) -> tuple[str, TodoList]:
        remote = self.containers.get(author)
        if remote is None:
            raise TodoError(f"user doesn't have any list.")

        if list_ident.isdigit():
            index = int(list_ident)
            self.validate_index(remote.todo, index)
            items = list(remote.todo.items())
            list_ident = items[index][0]
            rlist = items[index][1]
        else:
            rlist = remote.todo.get(list_ident)
            if rlist is None:
                raise TodoError(f"list with name: '{list_ident}' not found.")

        perms = rlist.editors.get(user)
        if user != author and (perms is None or not self.authorize(intents, perms)):
            raise TodoError("you are not authorized to access this list.")

        return list_ident, rlist

    def ensure_active_list(self, intents: Perms, container: TodoContainer) -> TodoList:
        if container.active is None:
            raise TodoError("no active list, try creating or activating a list first.")

        if container.active.author_id != container.author.author_id:
            author_container = self.containers.get(container.active.author_id)
            if author_container is None:
                raise TodoError("the owner of the active list no longer exists.")
            active = author_container.todo.get(container.active.name)
            if active is None:
                raise TodoError("the active list no longer exists.")
            perms = active.editors.get(container.author.author_id)
            if perms is None or not self.authorize(intents, perms):
                raise TodoError("you are not authorized to access this list.")
        else:
            active = container.todo.get(container.active.name)
            if active is None:
                raise TodoError("the active list no longer exists.")

        return active

    def validate_index(self, iterable, index: int):
        if not 0 <= index < len(iterable):
            raise TodoError(
                f"index {index} is out of bounds for iterable "
                f"containing {len(iterable)} elements."
            )

    def get_default_list_name(self, container: TodoContainer) -> str:
        name = "list"
        index = 0

        def make_new_name():
            nonlocal index
            index += 1
            return f"list#{index}"

        while container.todo.get(name) is not None:
            name = make_new_name()
            if container.todo.get(name) is None:
                return name

        return name

    @staticmethod
    def _next_task_id(lst: TodoList) -> int:
        task_id = int(time.time() * 1000)
        while any(task.id == task_id for task in lst.todo):
            task_id += 1
        return task_id

    def add_task(
        self,
        user: int,
        task: str,
        *,
        priority: str | None = None,
        due: float | None = None,
        origin: TaskOrigin | None = None,
    ) -> str:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(write=True), user_container)
        if len(active.todo) >= MAX_TASKS:
            raise TodoError(f"list is full (max tasks is {MAX_TASKS}).")

        priority = _check_priority(priority)
        due = _check_due(due)
        self._snapshot(user, "add", user_container.active.author_id)
        number, branch = self._next_slot(active)
        active.todo.append(
            TodoTask(
                content=task,
                author_id=user,
                id=self._next_task_id(active),
                priority=priority,
                due=due,
                origin=origin,
                number=number,
                branch=branch,
                created_at=time.time(),
            )
        )
        self._persist(user_container.active.author_id)
        return task_label(active.todo[-1])

    def remove_task(self, user: int, ref: str) -> tuple[str, str]:
        """v12: tasks are addressed by stable number (or tree label like
        46b). returns (label, content) of the removed task."""
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(remove=True), user_container)
        pos = self.resolve_task_index(active, ref)
        self._snapshot(user, "remove", user_container.active.author_id)
        task = active.todo.pop(pos)
        if task.tree and active.tree_root == task.number:
            active.tree_root = None  # the armed root itself was removed
        self._persist(user_container.active.author_id)
        return task_label(task), task.content

    def remove_tasks(self, user: int, start: int, end: int) -> int:
        """v12: the range is task NUMBERS (inclusive). tree branches
        ride their root's number, so a range covering a root takes its
        whole subtree. returns how many tasks went."""
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(remove=True), user_container)
        if start > end:
            raise TodoError("the range start must not be after its end.")
        doomed = [
            t for t in active.todo if t.number is not None and start <= t.number <= end
        ]
        if not doomed:
            raise TodoError(f"no tasks numbered {start}..{end} in this list.")
        self._snapshot(user, "removes", user_container.active.author_id)
        gone = {t.number for t in doomed}
        active.todo[:] = [
            t for t in active.todo if t.number is None or not start <= t.number <= end
        ]
        if active.tree_root is not None and active.tree_root in gone:
            active.tree_root = None
        self._persist(user_container.active.author_id)
        return len(doomed)

    def update_task(
        self,
        user: int,
        ref: str,
        task: str | object = UNSET,
        *,
        priority: str | None | object = UNSET,
        due: float | None | object = UNSET,
    ) -> tuple[str, str]:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(
            Perms(remove=True, write=True, read=True), user_container
        )
        pos = self.resolve_task_index(active, ref)
        self._snapshot(user, "update", user_container.active.author_id)
        old = active.todo[pos]
        if priority is not UNSET:
            priority = _check_priority(priority)
        if due is not UNSET:
            due = _check_due(due)
        updated = old.model_copy(
            update={
                "content": task if task is not UNSET else old.content,
                "priority": priority if priority is not UNSET else old.priority,
                "due": due if due is not UNSET else old.due,
                # a moved deadline re-arms the ping
                "reminded": False if due is not UNSET else old.reminded,
                "author_id": user,
                # v12: edit bookkeeping
                "edited_at": time.time(),
                "edited_count": old.edited_count + 1,
            }
        )
        active.todo[pos] = updated
        self._persist(user_container.active.author_id)
        return task_label(old), old.content

    def _locate(
        self, owner: int, list_name: str, task_id: int
    ) -> tuple[str, TodoList, int, TodoTask] | None:
        container = self.containers.get(owner)
        if container is None:
            return None
        lst = container.todo.get(list_name)
        if lst is None:
            return None
        for index, task in enumerate(lst.todo):
            if task.id == task_id:
                return list_name, lst, index, task
        return None

    def reschedule_task(
        self, owner: int, list_name: str, task_id: int, due: float, *, actor: int
    ) -> str:
        found = self._locate(owner, list_name, task_id)
        if found is None:
            raise TodoError("that task is gone: it was removed or the list changed.")
        _, lst, index, task = found
        user_container = self.ensure_container(actor)
        if actor != owner:
            perms = lst.editors.get(actor)
            if perms is None or not self.authorize(Perms(write=True), perms):
                raise TodoError("you are not authorized to edit this list.")
        due = _check_due(due)
        self._snapshot(actor, "reschedule", owner)
        lst.todo[index] = task.model_copy(
            update={
                "due": due,
                "reminded": False,
                "edited_at": time.time(),
                "edited_count": task.edited_count + 1,
            }
        )
        self._persist(owner)
        return task_preview_of(task)

    def finish_task(
        self, owner: int, list_name: str, task_id: int, *, actor: int
    ) -> str:
        found = self._locate(owner, list_name, task_id)
        if found is None:
            raise TodoError("that task is gone: it was removed or the list changed.")
        _, lst, index, task = found
        if actor != owner:
            perms = lst.editors.get(actor)
            if perms is None or not self.authorize(Perms(remove=True), perms):
                raise TodoError("you are not authorized to remove from this list.")
        self._snapshot(actor, "finish", owner)
        lst.todo.pop(index)
        self._persist(owner)
        return task_preview_of(task)

    def due_ready(self, *, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        fired: list[dict] = []
        for owner, container in self.containers.items():
            for name, lst in container.todo.items():
                for index, task in enumerate(lst.todo):
                    if task.due is None or task.reminded or task.due > now:
                        continue
                    lst.todo[index] = task.model_copy(update={"reminded": True})
                    self._persist(owner)
                    fired.append(
                        {
                            "owner": owner,
                            "list": name,
                            "task_id": task.id,
                            "task": task,
                        }
                    )
        return fired

    @staticmethod
    def sorted_tasks(lst: TodoList) -> list[tuple[int, TodoTask]]:
        ranked = sorted(
            enumerate(lst.todo),
            key=lambda pair: (
                PRIORITY_RANK.get(pair[1].priority, 3),
                0 if pair[1].due is not None else 1,
                pair[1].due or 0.0,
                pair[0],
            ),
        )
        return ranked

    def list_tasks(
        self, user: int, author: int, list_ident: str = ""
    ) -> tuple[str, TodoList]:
        user_container = self.ensure_container(user)
        if list_ident == "":
            lst = self.ensure_active_list(Perms(read=True), user_container)
            name = user_container.active.name  # pyright: ignore reason: handled
        else:
            name, lst = self.ensure_list(user, author, list_ident, Perms(read=True))
        if lst is None:
            raise TodoError(f"a list: '{list_ident}' does not exist.")
        return name, lst

    def create_list(
        self, user: int, set_active: bool, list_name: str = ""
    ) -> tuple[str, int]:
        user_container = self.ensure_container(user)
        if list_name == "":
            list_name = self.get_default_list_name(user_container)
        elif list_name.isdigit():
            raise TodoError("list name can't be a number.")
        else:
            if len(list_name) > MAX_LIST_NAME:
                raise TodoError(
                    f"the list name exceeds max list name which is: {MAX_LIST_NAME}, try picking a name in that range 😁."
                )
        if user_container.todo.get(list_name) is not None:
            raise TodoError(f"a list with the name: '{list_name}' already exists.")
        if len(user_container.todo) >= MAX_LISTS:
            raise TodoError(f"lists count exceeds limit which is: {MAX_LISTS}.")
        self._snapshot(user, "create", user)
        user_container.todo[list_name] = TodoList()
        if set_active:
            user_container.active = TodoListHandle(author_id=user, name=list_name)
        self._persist(user)
        return list_name, list(user_container.todo).index(list_name)

    def discard_list(self, user: int, list_ident: str) -> TodoList:
        user_container = self.ensure_container(user)
        if not list_ident.isdigit():
            if user_container.todo.get(list_ident) is None:
                raise TodoError(
                    f"a list with the name: '{list_ident}' does not exists."
                )
            name = list_ident
        else:
            dig = int(list_ident)
            self.validate_index(user_container.todo, dig)
            name = list(user_container.todo.keys())[dig]

        self._snapshot(user, "discard", user)
        discarded = user_container.todo.pop(name)
        if (
            user_container.active is not None
            and user_container.active.author_id == user
            and user_container.active.name == name
        ):
            user_container.active = None
        self._persist(user)
        return discarded

    def set_editor(self, user: int, editor: int, perms: Perms, list_ident: str = ""):
        if user == editor:
            raise TodoError("you are already the author of this list 😁.")
        user_container = self.ensure_container(user)
        if list_ident == "":
            if user_container.active is None:
                raise TodoError("you don't have an active list.")
            if user_container.active.author_id != user:
                raise TodoError(
                    "you don't have authority to set editors for the active list."
                )
            list_ident = user_container.active.name

        name, lst = self.ensure_list(user, user, list_ident, perms)
        self._snapshot(user, "set-editor", user, editor)
        lst.editors[editor] = perms
        editor_in = self.ensure_container(editor).author.editor_in
        isset = False
        for handle in editor_in:
            if handle.handle.author_id == user and handle.handle.name == name:
                handle.perms = perms
                isset = True
        if not isset:
            editor_in.append(
                EditorHandle(
                    handle=TodoListHandle(author_id=user, name=name), perms=perms
                )
            )
        self._persist(user)
        self._persist(editor)

    def remove_editor(self, user: int, editor: int, list_ident: str = ""):
        if user == editor:
            raise TodoError("you are the author of this list 😁.")

        user_container = self.ensure_container(user)
        if list_ident == "":
            if user_container.active is None:
                raise TodoError("you don't have an active list.")
            if user_container.active.author_id != user:
                raise TodoError(
                    "you don't have authority to set editors for the active list."
                )
            list_ident = user_container.active.name
        name, lst = self.ensure_list(user, user, list_ident, Perms(read=False))
        self._snapshot(user, "remove-editor", user, editor)
        e = lst.editors.pop(editor, None)
        has_err = False
        if e is None:
            has_err = True
        editor_in = self.ensure_container(editor).author.editor_in
        for i, handle in enumerate(editor_in):
            if handle.handle.author_id == user and handle.handle.name == name:
                editor_in.pop(i)
                break
        self._persist(user)
        self._persist(editor)
        if has_err:
            raise TodoError(f"user: <@{editor}> isn't even an editor to begin with 😁")

    def list_lists(self, user: int) -> dict[str, TodoList]:
        user_container = self.ensure_container(user)
        return user_container.todo

    def set_excluded(self, user: int, list_ident: str, excluded: bool) -> str:
        """v10.9: mark a list as hoard material - it stops counting
        towards the `>todo lists` total (the list itself stays intact)."""
        name, lst = self.ensure_list(user, user, list_ident, Perms(write=True))
        self._snapshot(user, "exclude" if excluded else "include", user)
        lst.excluded = excluded
        self._persist(user)
        return name

    def add_reminder(
        self, user: int, content: str, due: float, origin: TaskOrigin | None
    ) -> int:
        """v10.9: `>remind` - a task on a dedicated `reminders` list that
        never touches (or needs) the user's active list."""
        user_container = self.ensure_container(user)
        lst = user_container.todo.get(REMINDERS_LIST)
        if lst is None:
            self._snapshot(user, "create", user)
            user_container.todo[REMINDERS_LIST] = TodoList()
            lst = user_container.todo[REMINDERS_LIST]
        if len(lst.todo) >= MAX_TASKS:
            raise TodoError(f"reminders list is full (max tasks is {MAX_TASKS}).")
        self._snapshot(user, "remind", user)
        number, branch = self._next_slot(lst)
        lst.todo.append(
            TodoTask(
                content=content,
                author_id=user,
                id=self._next_task_id(lst),
                due=_check_due(due),
                origin=origin,
                number=number,
                branch=branch,
                created_at=time.time(),
            )
        )
        self._persist(user)
        return task_label(lst.todo[-1])

    # -- v12: stable numbers, trees, task metadata -----------------------

    def resolve_task_index(self, lst: TodoList, ref) -> int:
        """a task reference is its number ("12") or its tree label
        ("46b"). returns the task's position in the list."""
        raw = str(ref).strip().strip('"').strip("'").lower()
        match = TASK_REF_RE.match(raw)
        if not match:
            raise TodoError(
                f"'{ref}' is not a task number (like 12, or a tree " "branch like 46b)."
            )
        number, branch = int(match.group(1)), match.group(2)
        for pos, task in enumerate(lst.todo):
            if task.number == number and task.branch == branch:
                return pos
        raise TodoError(
            f"no task {raw} in this list - the numbers live in `>todo list`."
        )

    def _next_slot(self, lst: TodoList) -> tuple[int, str]:
        """the (number, branch) the next add on this list gets: a branch
        of the armed tree (if its root still exists), else a fresh
        number."""
        root = lst.tree_root
        if root is not None:
            if any(t.tree and t.number == root for t in lst.todo):
                return root, _branch_next(_branch_last(lst, root))
            lst.tree_root = None  # the armed root is gone: fall back
        number = lst.seq
        lst.seq = number + 1
        return number, ""

    def start_tree(self, user: int, name: str) -> tuple[str, bool, str]:
        """start a new tree (its root task is created with `name`), or
        resume an existing one by number or name. returns
        (root_label, resumed, root_content)."""
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(write=True), user_container)
        raw = str(name).strip()
        if not raw:
            raise TodoError(
                "a tree needs a name - or an existing tree's number or "
                "name to resume it."
            )
        target = None
        if raw.isdigit():
            number = int(raw)
            target = next(
                (t for t in active.todo if t.tree and t.number == number), None
            )
            if target is None and any(t.number == number for t in active.todo):
                raise TodoError(
                    f"task {number} is not a tree - trees grow from "
                    "`>tree start <name>`."
                )
        if target is None:
            target = next(
                (
                    t
                    for t in active.todo
                    if t.tree and t.content.strip().lower() == raw.lower()
                ),
                None,
            )
        if target is not None:
            self._snapshot(user, "tree-resume", user_container.active.author_id)
            active.tree_root = target.number
            self._persist(user_container.active.author_id)
            return task_label(target), True, target.content
        self._snapshot(user, "tree-start", user_container.active.author_id)
        number, _branch = self._next_slot(active)
        active.todo.append(
            TodoTask(
                content=raw,
                author_id=user,
                id=self._next_task_id(active),
                number=number,
                tree=True,
                created_at=time.time(),
            )
        )
        active.tree_root = number
        self._persist(user_container.active.author_id)
        return task_label(active.todo[-1]), False, raw

    def stop_tree(self, user: int) -> str:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(write=True), user_container)
        root = active.tree_root
        if root is None:
            raise TodoError("no tree is active - start one with `>tree start <name>`.")
        self._snapshot(user, "tree-stop", user_container.active.author_id)
        active.tree_root = None
        self._persist(user_container.active.author_id)
        return str(root)

    def list_trees(self, user: int) -> list[dict]:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(read=True), user_container)
        out = []
        for task in active.todo:
            if not task.tree:
                continue
            out.append(
                {
                    "label": task_label(task),
                    "content": task.content,
                    "branches": sum(
                        1 for t in active.todo if t.number == task.number and t.branch
                    ),
                    "active": active.tree_root == task.number,
                    "created_at": task.created_at,
                }
            )
        return out

    def task_info(self, user: int, author: int, list_ident: str, ref: str) -> dict:
        """v12: everything known about one task - content, priority, due,
        creation/edit stamps, tree membership."""
        name, lst = self.list_tasks(user, author, list_ident)
        pos = self.resolve_task_index(lst, ref)
        task = lst.todo[pos]
        tree: dict | None = None
        if task.tree:
            tree = {
                "role": "root",
                "label": task_label(task),
                "content": task.content,
                "branches": sum(
                    1 for t in lst.todo if t.number == task.number and t.branch
                ),
                "active": lst.tree_root == task.number,
            }
        elif task.branch:
            root = next(
                (t for t in lst.todo if t.tree and t.number == task.number),
                None,
            )
            tree = {
                "role": "branch",
                "label": str(task.number),
                "content": root.content if root else "(root removed)",
            }
        return {
            "list": name,
            "label": task_label(task),
            "content": task.content,
            "author_id": task.author_id,
            "priority": task.priority,
            "due": task.due,
            "reminded": task.reminded,
            "created_at": task.created_at,
            "edited_at": task.edited_at,
            "edited_count": task.edited_count,
            "tree": tree,
        }

    # -- per-user prefs (timezone for now) ---------------------------------

    def tz_of(self, user: int) -> str | None:
        prefs = self.store.get(NS_PREFS, str(user)) or {}
        name = prefs.get("tz") if isinstance(prefs, dict) else None
        return str(name) if name else None

    def set_tz(self, user: int, name: str | None) -> None:
        prefs = self.store.get(NS_PREFS, str(user)) or {}
        if not isinstance(prefs, dict):
            prefs = {}
        if name:
            prefs["tz"] = name
        else:
            prefs.pop("tz", None)
        self.store.put(NS_PREFS, str(user), prefs)

    def list_contributions(self, user: int) -> list[EditorHandle]:
        user_container = self.ensure_container(user)
        return user_container.author.editor_in

    def activate_list(self, user: int, list_ident: str, author: int):
        user_container = self.ensure_container(user)
        name, lst = self.ensure_list(user, author, list_ident, Perms(read=True))
        self._snapshot(user, "activate", user)
        user_container.active = TodoListHandle(author_id=author, name=name)
        self._persist(user)

    def deactivate_list(self, user: int):
        user_container = self.ensure_container(user)
        self._snapshot(user, "deactivate", user)
        user_container.active = None
        self._persist(user)


def task_preview_of(task: TodoTask, limit: int = 25) -> str:
    content = task.content
    return content if len(content) < limit else f"{content[:limit - 3]}..."
