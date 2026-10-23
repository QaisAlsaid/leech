import os
import shutil
import tempfile

import pytest

from leech.core.store import reset_store
from leech.cogs.todo.service import (
    TodoList,
    TodoTask,
    REMINDERS_LIST,
    TaskOrigin,
)


@pytest.fixture()
def store_dir(monkeypatch):
    work = tempfile.mkdtemp(prefix="todo-tests-")
    monkeypatch.setenv("LEECH_STORE", "json")
    monkeypatch.setenv("LEECH_STORE_DIR", work)
    reset_store()
    yield work
    reset_store()
    shutil.rmtree(work, ignore_errors=True)


def _service():
    from leech.cogs.todo.service import TodoService

    return TodoService()


def _container_of(svc, user):
    svc.ensure_container(user)
    return svc.containers[user]


def test_list_order_survives_key_shuffle(store_dir):
    # postgres jsonb reorders object keys; arrays survive. the container
    # must come back in `list_order` even when the dict arrives shuffled.
    svc = _service()
    c = _container_of(svc, 1)
    for name in ("alpha", "beta", "gamma-longer", "delta"):
        c.todo[name] = TodoList()
    svc._persist(1)
    order = list(c.todo.keys())

    raw = svc.store.get("todo", "1")
    mangled = sorted(raw["todo"].keys(), key=lambda k: (len(k), k.encode()))
    assert mangled != order  # the shuffle is real
    raw["todo"] = {k: raw["todo"][k] for k in mangled}
    svc.store.put("todo", "1", raw)

    svc2 = _service()
    assert list(svc2.containers[1].todo.keys()) == order
    assert svc2.containers[1].list_order == order


def test_numeric_list_indices_are_stable(store_dir):
    from leech.cogs.todo.service import Perms

    svc = _service()
    c = _container_of(svc, 1)
    for name in ("alpha", "beta"):
        c.todo[name] = TodoList()
    svc._persist(1)

    raw = svc.store.get("todo", "1")
    raw["todo"] = {k: raw["todo"][k] for k in ("beta", "alpha")}  # shuffled
    svc.store.put("todo", "1", raw)

    svc2 = _service()
    name, _ = svc2.ensure_list(1, 1, "0", Perms(read=True))
    assert name == "alpha"


def test_new_lists_append_and_discarded_drop(store_dir):
    svc = _service()
    c = _container_of(svc, 1)
    c.todo["one"] = TodoList()
    svc._persist(1)
    svc2 = _service()
    _container_of(svc2, 1).todo["two"] = TodoList()
    svc2._persist(1)
    svc3 = _service()
    assert list(svc3.containers[1].todo.keys()) == ["one", "two"]
    svc3.discard_list(1, "one")
    svc4 = _service()
    assert list(svc4.containers[1].todo.keys()) == ["two"]


def test_excluded_lists_and_counter_split(store_dir):
    svc = _service()
    _ = _container_of(svc, 1)
    svc.create_list(1, False, "main")
    svc.create_list(1, False, "hoard")
    svc.ensure_container(1).todo["main"].todo.append(TodoTask(content="a"))
    svc._persist(1)

    svc.set_excluded(1, "hoard", True)
    svc2 = _service()
    lists = svc2.list_lists(1)
    assert lists["hoard"].excluded is True
    assert lists["main"].excluded is False
    svc2.set_excluded(1, "hoard", False)
    svc3 = _service()
    assert svc3.list_lists(1)["hoard"].excluded is False


def test_remind_uses_dedicated_list(store_dir):
    import time

    svc = _service()
    svc.add_reminder(
        1, "call mom", time.time() + 3600, TaskOrigin(channel_id=9, user_id=1)
    )
    c = svc.containers[1]
    assert REMINDERS_LIST in c.todo
    assert c.todo[REMINDERS_LIST].todo[0].content == "call mom"
    assert c.todo[REMINDERS_LIST].todo[0].due is not None
    assert c.active is None  # the user's active list is never touched

    fired = svc.due_ready(now=time.time() + 7200)
    assert any(f["list"] == REMINDERS_LIST for f in fired)


def test_per_user_tz_prefs(store_dir):
    svc = _service()
    assert svc.tz_of(1) is None
    svc.set_tz(1, "Asia/Damascus")
    svc2 = _service()
    assert svc2.tz_of(1) == "Asia/Damascus"
    svc2.set_tz(1, None)
    assert _service().tz_of(1) is None
