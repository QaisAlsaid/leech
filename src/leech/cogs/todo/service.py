from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import json

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
MAX_TASKS = 500
MAX_LISTS = 100


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

        active.todo.append(TodoTask(content=task, author_id=user))
        self.persist_json()
        return len(active.todo) - 1

    def remove_task(self, user: int, task_index: int) -> str:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(Perms(remove=True), user_container)
        self.validate_index(active.todo, task_index)
        task = active.todo.pop(task_index)
        self.persist_json()
        return task.content

    def update_task(self, user: int, task_index: int, task: str) -> str:
        user_container = self.ensure_container(user)
        active = self.ensure_active_list(
            Perms(remove=True, write=True, read=True), user_container
        )
        self.validate_index(active.todo, task_index)
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
        user_container.active = TodoListHandle(author_id=author, name=name)
        self.persist_json()

    def deactivate_list(self, user: int):
        user_container = self.ensure_container(user)
        user_container.active = None
        self.persist_json()
