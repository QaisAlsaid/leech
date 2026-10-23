from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import json
import re
import time

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from leech.core.error import BaseError
from leech.core.cog.error import CogError


class TodoTask(BaseModel):
    content: str
    author_id: int | None = None


class Perms(BaseModel):
    read: bool = True
    write: bool = False
    remove: bool = False


class TodoList(BaseModel):
    todo: list[TodoTask] = Field(default_factory=list)
    editors: dict[int, Perms] = Field(default_factory=dict)


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


container_adapter = TypeAdapter(dict[int, TodoContainer])
MAX_LIST_NAME = 100
MAX_TASKS = 1000
MAX_LISTS = 100

# v10.6 safety net: every mutation snapshots the affected container(s)
# first. snapshots double as the undo ring and the mutation log; a full
# backup of todo.json lands every BACKUP_EVERY mutations (plus manual
# ones). this is the "the AI removed tasks from the wrong list at 2am"
# recovery path - see >todo undo / >todo log / >todo restore.
UNDO_DIR = Path("persistent/v1/todo/undo")
UNDO_KEEP = 30
BACKUP_DIR = Path("persistent/v1/todo/backups")
BACKUP_EVERY = 25
BACKUP_KEEP = 12
BACKUP_NAME_RE = re.compile(r"^\d{8}-\d{6}\.json$")


class TodoError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


class TodoService:
    def __init__(self):
        self.path = Path("persistent/v1/todo/todo.json")
        self.temppath = Path("persistent/v1/todo/todo.json.temp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.containers: dict[int, TodoContainer] = container_adapter.validate_python(
            self.load_json()
        )
        self._mutations = 0

    # ------------------------------------------------------------ history

    def _snapshot(self, actor: int, action: str, *owners: int) -> None:
        """called BEFORE every mutating operation. writes one undo entry
        per affected owner container. the entries are also the mutation
        log (actor + action + time), so `>todo log` answers "who touched
        my lists" - including when the AI did it through the dispatcher."""
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
            directory = UNDO_DIR / str(owner)
            directory.mkdir(parents=True, exist_ok=True)
            seq = int(time.time() * 1000)
            path = directory / f"{seq}.json"
            while path.exists():
                seq += 1
                path = directory / f"{seq}.json"
            path.write_text(json.dumps(entry), encoding="utf-8")
            entries = sorted(directory.glob("*.json"))
            for stale in entries[:-UNDO_KEEP]:
                stale.unlink(missing_ok=True)
        self._mutations += 1
        if self._mutations % BACKUP_EVERY == 0:
            try:
                self.backup()
            except TodoError:
                pass  # the mutation itself already persisted; retry next time

    def _undo_entries(self, user: int) -> list[Path]:
        directory = UNDO_DIR / str(user)
        if not directory.is_dir():
            return []
        return sorted(directory.glob("*.json"))

    def undo(self, user: int) -> dict | None:
        """restore the invoking user's container to its state before its
        most recent mutation (whatever/whoever caused it). returns the
        undone mutation's meta, or None when there's nothing to undo.
        undoing does NOT push a new snapshot - undoing twice walks two
        steps back - but the next real mutation prunes the redone path."""
        entries = self._undo_entries(user)
        if not entries:
            return None
        path = entries[-1]
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            path.unlink(missing_ok=True)
            raise TodoError(f"an undo entry was unreadable and got dropped: {e}")
        state = entry.get("state")
        if state is None:
            self.containers.pop(user, None)
        else:
            self.containers[user] = TodoContainer.model_validate(state)
        self.persist_json()
        path.unlink(missing_ok=True)
        return {
            "action": entry.get("action") or "?",
            "actor": entry.get("actor"),
            "at": entry.get("at"),
        }

    def log(self, user: int, limit: int = 10) -> list[dict]:
        """newest-first mutation meta for the user's container."""
        out: list[dict] = []
        for path in reversed(self._undo_entries(user)[-max(1, limit):]):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({
                "action": entry.get("action") or "?",
                "actor": entry.get("actor"),
                "at": entry.get("at"),
            })
        return out

    def backup(self) -> str:
        """write a full todo.json copy into the backup dir. returns the
        backup name."""
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + ".json"
        data = json.dumps(
            container_adapter.dump_python(self.containers, mode="json"),
            ensure_ascii=False,
        )
        (BACKUP_DIR / name).write_text(data, encoding="utf-8")
        for stale in sorted(BACKUP_DIR.glob("*.json"))[:-BACKUP_KEEP]:
            stale.unlink(missing_ok=True)
        return name

    def backups(self) -> list[str]:
        if not BACKUP_DIR.is_dir():
            return []
        return [p.name for p in sorted(BACKUP_DIR.glob("*.json"))]

    def restore_backup(self, name: str) -> int:
        """replace the whole store with a backup (owner-only command -
        it hits every user's data). returns the containers count."""
        if not BACKUP_NAME_RE.fullmatch(name or ""):
            raise TodoError(
                "backup name must look like 20261022-025059.json - pick one "
                "from `>todo restore`."
            )
        path = BACKUP_DIR / name
        if not path.is_file():
            raise TodoError(f"no backup named {name} - check `>todo restore`. ")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            containers = container_adapter.validate_python(data)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            raise TodoError(f"backup {name} is unreadable: {e}")
        self.containers = containers
        self.persist_json()
        return len(containers)

    # ------------------------------------------------------------ mutators

    def load_json(self):
        if not self.path.exists():
            return {}

        with self.path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def persist_json(self):
        try:
            with self.temppath.open("w", encoding="utf-8") as file:
                json.dump(
                    container_adapter.dump_python(self.containers, mode="json"),
                    file,
                )

            os.replace(self.temppath, self.path)
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

    def add_task(self, user: int, task: str) -> int:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(write=True), user_container)
        if len(active.todo) >= MAX_TASKS:
            raise TodoError(f"list is full (max tasks is {MAX_TASKS}).")

        self._snapshot(user, "add", user_container.active.author_id)
        active.todo.append(TodoTask(content=task, author_id=user))
        self.persist_json()
        return len(active.todo) - 1

    def remove_task(self, user: int, task_index: int) -> str:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(remove=True), user_container)
        self.validate_index(active.todo, task_index)
        self._snapshot(user, "remove", user_container.active.author_id)
        task = active.todo.pop(task_index)
        self.persist_json()
        return task.content

    def update_task(self, user: int, task_index: int, task: str) -> str:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(
            Perms(remove=True, write=True, read=True), user_container
        )
        self.validate_index(active.todo, task_index)
        self._snapshot(user, "update", user_container.active.author_id)
        old = active.todo[task_index]
        active.todo[task_index] = TodoTask(content=task, author_id=user)
        self.persist_json()
        return old.content

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
        self.persist_json()
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
        self.persist_json()
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
        self.persist_json()

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
        self.persist_json()
        if has_err:
            raise TodoError(f"user: <@{editor}> isn't even an editor to begin with 😁")

    def list_lists(self, user: int) -> dict[str, TodoList]:
        user_container = self.ensure_container(user)
        return user_container.todo

    def list_contributions(self, user: int) -> list[EditorHandle]:
        user_container = self.ensure_container(user)
        return user_container.author.editor_in

    def activate_list(self, user: int, list_ident: str, author: int):
        user_container = self.ensure_container(user)
        name, lst = self.ensure_list(user, author, list_ident, Perms(read=True))
        self._snapshot(user, "activate", user)
        user_container.active = TodoListHandle(author_id=author, name=name)
        self.persist_json()

    def deactivate_list(self, user: int):
        user_container = self.ensure_container(user)
        self._snapshot(user, "deactivate", user)
        user_container.active = None
        self.persist_json()
