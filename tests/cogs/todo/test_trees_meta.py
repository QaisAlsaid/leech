"""v12: stable task numbers, tree todos, and task metadata."""

import shutil
import tempfile
import time

import pytest

from leech.core.store import reset_store
from leech.cogs.todo.service import (
    TodoError,
    TodoList,
    TodoService,
    TodoTask,
    _branch_next,
    reconcile_task_numbers,
    task_label,
)


@pytest.fixture()
def store_dir(monkeypatch):
    work = tempfile.mkdtemp(prefix="todo-v12-tests-")
    monkeypatch.setenv("LEECH_STORE", "json")
    monkeypatch.setenv("LEECH_STORE_DIR", work)
    reset_store()
    yield work
    reset_store()
    shutil.rmtree(work, ignore_errors=True)


def _service():
    return TodoService()


def _list_of(svc, user, name="main"):
    container = svc.ensure_container(user)
    lst = container.todo.get(name)
    if lst is None:
        svc.create_list(user, True, name)
        lst = container.todo[name]
    return lst


# -- stable numbers -------------------------------------------------------


def test_numbers_are_stable_across_removal(store_dir):
    svc = _service()
    _list_of(svc, 1)
    assert [svc.add_task(1, "a"), svc.add_task(1, "b"), svc.add_task(1, "c")] == [
        "1",
        "2",
        "3",
    ]
    svc.remove_task(1, "2")
    assert svc.add_task(1, "d") == "4"  # never reused
    labels = [task_label(t) for t in _list_of(svc, 1).todo]
    assert labels == ["1", "3", "4"]


def test_legacy_rows_get_numbers_on_load(store_dir):
    svc = _service()
    lst = _list_of(svc, 1)
    ms = 1790000000000
    for i, content in enumerate(("old a", "old b", "old c")):
        lst.todo.append(TodoTask(content=content, id=ms + i))
    svc._persist(1)

    svc2 = _service()
    lst2 = svc2.containers[1].todo["main"]
    assert [t.number for t in lst2.todo] == [1, 2, 3]
    assert lst2.seq == 4
    # creation stamps backfilled from the ms id
    assert all(t.created_at is not None for t in lst2.todo)
    assert svc2.add_task(1, "new") == "4"


def test_reconcile_keeps_existing_numbers(store_dir):
    lst = TodoList()
    lst.todo = [
        TodoTask(content="keep", number=5),
        TodoTask(content="legacy"),
    ]
    reconcile_task_numbers(lst)
    numbers = sorted(t.number for t in lst.todo)
    assert 5 in numbers and len(set(numbers)) == 2
    assert lst.seq == 6


def test_reconcile_ignores_implausible_ids(store_dir):
    lst = TodoList()
    lst.todo = [TodoTask(content="tiny id", id=7)]
    reconcile_task_numbers(lst)
    assert lst.todo[0].created_at is None


# -- trees -----------------------------------------------------------------


def test_tree_full_lifecycle(store_dir):
    svc = _service()
    _list_of(svc, 1)
    for i in range(45):
        svc.add_task(1, f"filler {i}")
    assert svc.add_task(1, "plain before") == "46"

    label, resumed, content = svc.start_tree(1, "oklang rewrite")
    assert (label, resumed, content) == ("47", False, "oklang rewrite")
    assert svc.add_task(1, "step one") == "47a"
    assert svc.add_task(1, "step two") == "47b"
    assert svc.add_task(1, "step three") == "47c"

    assert svc.stop_tree(1) == "47"
    assert svc.add_task(1, "after stop") == "48"  # the friend's 47

    label, resumed, _ = svc.start_tree(1, "47")  # resume by number
    assert (label, resumed) == ("47", True)
    assert svc.add_task(1, "step four") == "47d"
    svc.stop_tree(1)

    label, resumed, _ = svc.start_tree(1, "OKLANG REWRITE")  # by name
    assert (label, resumed) == ("47", True)
    assert svc.add_task(1, "step five") == "47e"
    svc.stop_tree(1)

    trees = svc.list_trees(1)
    assert len(trees) == 1
    assert trees[0]["label"] == "47" and trees[0]["branches"] == 5


def test_tree_resume_rejects_plain_tasks(store_dir):
    svc = _service()
    _list_of(svc, 1)
    svc.add_task(1, "just a task")
    with pytest.raises(TodoError):
        svc.start_tree(1, "1")


def test_tree_letters_go_past_z(store_dir):
    assert _branch_next("") == "a"
    assert _branch_next("z") == "aa"
    assert _branch_next("az") == "ba"
    assert _branch_next("zz") == "aaa"

    svc = _service()
    _list_of(svc, 1)
    svc.start_tree(1, "big tree")
    for i in range(27):
        svc.add_task(1, f"branch {i}")
    branches = [t.branch for t in _list_of(svc, 1).todo if t.branch]
    assert branches[-1] == "aa"
    svc.stop_tree(1)
    assert svc.add_task(1, "fresh") == "2"  # branches never consumed numbers


def test_tree_is_scoped_to_its_list(store_dir):
    svc = _service()
    _list_of(svc, 1, "main")
    svc.create_list(1, False, "other")  # don't steal the active slot
    svc.start_tree(1, "main tree")  # armed on "main"
    svc.activate_list(1, "other", 1)
    assert svc.add_task(1, "elsewhere") == "1"  # other list unaffected
    svc.activate_list(1, "main", 1)
    assert svc.add_task(1, "on main") == "1a"  # main's tree still grows


def test_armed_tree_root_removed_falls_back(store_dir):
    svc = _service()
    _list_of(svc, 1)
    label, _, _ = svc.start_tree(1, "ghost tree")
    svc.remove_task(1, label)  # root gone while armed
    assert svc.add_task(1, "fallback").isdigit()
    assert _list_of(svc, 1).tree_root is None


def test_removes_range_takes_whole_subtree(store_dir):
    svc = _service()
    _list_of(svc, 1)
    assert svc.add_task(1, "one") == "1"
    svc.start_tree(1, "root")  # number 2
    svc.add_task(1, "2a")
    svc.add_task(1, "2b")
    svc.stop_tree(1)
    assert svc.add_task(1, "three") == "3"
    assert svc.remove_tasks(1, 2, 3) == 4  # root + 2 branches + 3
    labels = [task_label(t) for t in _list_of(svc, 1).todo]
    assert labels == ["1"]
    assert svc.add_task(1, "four") == "4"  # numbers never reused


def test_removes_empty_range_errors(store_dir):
    svc = _service()
    _list_of(svc, 1)
    svc.add_task(1, "only")
    with pytest.raises(TodoError):
        svc.remove_tasks(1, 5, 9)
    with pytest.raises(TodoError):
        svc.remove_tasks(1, 9, 5)


def test_reminders_list_gets_numbers_too(store_dir):
    import time as _time

    svc = _service()
    label = svc.add_reminder(1, "call mom", _time.time() + 3600, None)
    assert label == "1"
    assert svc.add_reminder(1, "call dad", _time.time() + 3600, None) == "2"


# -- task references -------------------------------------------------------


def test_resolve_task_ref_forms(store_dir):
    svc = _service()
    lst = _list_of(svc, 1)
    svc.start_tree(1, "root")  # 1
    svc.add_task(1, "branch")  # 1a
    svc.stop_tree(1)
    svc.add_task(1, "plain")  # 2

    assert lst.todo[svc.resolve_task_index(lst, "1")].tree is True
    assert lst.todo[svc.resolve_task_index(lst, "1a")].branch == "a"
    assert lst.todo[svc.resolve_task_index(lst, "2")].content == "plain"
    assert lst.todo[svc.resolve_task_index(lst, "'2'")].content == "plain"
    with pytest.raises(TodoError):
        svc.resolve_task_index(lst, "1x")
    with pytest.raises(TodoError):
        svc.resolve_task_index(lst, "nope")
    with pytest.raises(TodoError):
        svc.resolve_task_index(lst, "3")  # not there


# -- metadata ---------------------------------------------------------------


def test_task_metadata_stamps(store_dir):
    svc = _service()
    _list_of(svc, 1)
    before = time.time()
    svc.add_task(1, "original")
    info = svc.task_info(1, 1, "", "1")
    assert info["created_at"] >= before
    assert info["edited_at"] is None
    assert info["edited_count"] == 0
    assert info["author_id"] == 1

    svc.update_task(1, "1", "edited once", priority="high")
    info = svc.task_info(1, 1, "", "1")
    assert info["edited_at"] >= before
    assert info["edited_count"] == 1
    assert info["priority"] == "high"
    assert info["content"] == "edited once"

    svc.update_task(1, "1", due=1790000000.0)
    info = svc.task_info(1, 1, "", "1")
    assert info["edited_count"] == 2


def test_snooze_counts_as_edit(store_dir):
    svc = _service()
    _list_of(svc, 1)
    svc.add_task(1, "with due", due=1790000000.0)
    lst = _list_of(svc, 1)
    task = lst.todo[0]
    svc.reschedule_task(1, "main", task.id, 1790009999.0, actor=1)
    refreshed = svc.containers[1].todo["main"].todo[0]
    assert refreshed.edited_count == 1
    assert refreshed.edited_at is not None
    assert refreshed.reminded is False


def test_task_info_tree_roles(store_dir):
    svc = _service()
    _list_of(svc, 1)
    svc.start_tree(1, "the tree")
    svc.add_task(1, "a branch")
    svc.stop_tree(1)

    root_info = svc.task_info(1, 1, "", "1")
    assert root_info["tree"]["role"] == "root"
    assert root_info["tree"]["branches"] == 1
    assert root_info["tree"]["active"] is False

    branch_info = svc.task_info(1, 1, "", "1a")
    assert branch_info["tree"]["role"] == "branch"
    assert branch_info["tree"]["label"] == "1"
    assert branch_info["tree"]["content"] == "the tree"

    # orphaned branch: root removed, info still renders
    svc.remove_task(1, "1")
    orphan = svc.task_info(1, 1, "", "1a")
    assert orphan["tree"]["content"] == "(root removed)"


# -- persistence round-trip --------------------------------------------------


def test_numbers_and_trees_survive_reload(store_dir):
    svc = _service()
    _list_of(svc, 1)
    svc.start_tree(1, "durable tree")  # 1
    svc.add_task(1, "d1")
    svc.add_task(1, "d2")
    # leave it armed - the armed state itself must persist
    label = svc.add_task(1, "d3")
    assert label == "1c"

    svc2 = _service()
    lst = svc2.containers[1].todo["main"]
    assert lst.tree_root == 1
    assert [task_label(t) for t in lst.todo] == ["1", "1a", "1b", "1c"]
    assert svc2.add_task(1, "still growing") == "1d"


def test_undo_restores_numbers_and_tree_state(store_dir):
    svc = _service()
    _list_of(svc, 1)
    svc.start_tree(1, "undoable")
    svc.add_task(1, "branch one")
    svc.undo(1)  # undo the add
    assert [task_label(t) for t in _list_of(svc, 1).todo] == ["1"]
    assert _list_of(svc, 1).tree_root == 1
    assert svc.add_task(1, "branch again") == "1a"
