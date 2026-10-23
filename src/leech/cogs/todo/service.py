from pydantic import BaseModel, Field, TypeAdapter, field_validator
from leech.core.error import BaseError
from leech.core.store import Store, get_store
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
    id: int | None = None          # creation order identity (ms stamp)
    priority: str | None = None    # high | medium | low | None
    due: float | None = None       # unix seconds
    reminded: bool = False         # ping fired (and was answered or given up)
    origin: TaskOrigin | None = None

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
                value if isinstance(value, TodoContainer)
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
                    if owner in self.containers else None
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
            key for key in self.store.items(NS_UNDO)
            if key.startswith(prefix)
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
        for key in reversed(self._undo_entries(user)[-max(1, limit):]):
            entry = self.store.get(NS_UNDO, key) or {}
            out.append({
                "action": entry.get("action") or "?",
                "actor": entry.get("actor"),
                "at": entry.get("at"),
            })
        return out

    def backup(self) -> str:
        name = time.strftime("%Y%m%d-%H%M%S")
        for container in self.containers.values():
            reconcile_order(container)
        self.store.put(
            NS_BACKUP,
            name,
            {
                "containers": container_adapter.dump_python(
                    self.containers, mode="json"
                )
            },
        )
        for stale in sorted(self.store.items(NS_BACKUP))[:-BACKUP_KEEP]:
            self.store.delete(NS_BACKUP, stale)
        return name

    def backups(self) -> list[str]:
        return sorted(self.store.items(NS_BACKUP))

    def restore_backup(self, name: str) -> int:
        import re

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
    ) -> int:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(write=True), user_container)
        if len(active.todo) >= MAX_TASKS:
            raise TodoError(f"list is full (max tasks is {MAX_TASKS}).")

        priority = _check_priority(priority)
        due = _check_due(due)
        self._snapshot(user, "add", user_container.active.author_id)
        active.todo.append(
            TodoTask(
                content=task,
                author_id=user,
                id=self._next_task_id(active),
                priority=priority,
                due=due,
                origin=origin,
            )
        )
        self._persist(user_container.active.author_id)
        return len(active.todo) - 1

    def remove_task(self, user: int, task_index: int) -> str:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(remove=True), user_container)
        self.validate_index(active.todo, task_index)
        self._snapshot(user, "remove", user_container.active.author_id)
        task = active.todo.pop(task_index)
        self._persist(user_container.active.author_id)
        return task.content

    def update_task(
        self,
        user: int,
        task_index: int,
        task: str | object = UNSET,
        *,
        priority: str | None | object = UNSET,
        due: float | None | object = UNSET,
    ) -> str:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(
            Perms(remove=True, write=True, read=True), user_container
        )
        self.validate_index(active.todo, task_index)
        self._snapshot(user, "update", user_container.active.author_id)
        old = active.todo[task_index]
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
            }
        )
        active.todo[task_index] = updated
        self._persist(user_container.active.author_id)
        return old.content

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
            update={"due": due, "reminded": False}
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
        lst.todo.append(
            TodoTask(
                content=content,
                author_id=user,
                id=self._next_task_id(lst),
                due=_check_due(due),
                origin=origin,
            )
        )
        self._persist(user)
        return len(lst.todo) - 1

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
