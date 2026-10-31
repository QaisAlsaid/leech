"""verify battery for task 46 (v12: task metadata + tree todos).

runs the checks that matter for this round end-to-end against a scratch
store, then the repo test suite. green = ship.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn

    return deco


def _fresh_service():
    from leech.core.store import reset_store
    from leech.cogs.todo.service import TodoService

    work = tempfile.mkdtemp(prefix="verify46-")
    os.environ["LEECH_STORE"] = "json"
    os.environ["LEECH_STORE_DIR"] = work
    reset_store()
    return TodoService(), work


@check("the friend's exact scenario")
def _():
    svc, work = _fresh_service()
    svc.create_list(1, True, "list")
    for i in range(45):
        svc.add_task(1, f"filler {i}")

    label, resumed, _ = svc.start_tree(1, "oklang rewrite")  # task 46
    assert label == "46" and resumed is False
    assert svc.add_task(1, "a") == "46a"
    assert svc.add_task(1, "b") == "46b"
    assert svc.add_task(1, "c") == "46c"
    assert svc.stop_tree(1) == "46"
    assert svc.add_task(1, "plain") == "47"  # "makes the next task made 47"
    label, resumed, _ = svc.start_tree(1, "46")  # resume by number
    assert label == "46" and resumed is True
    assert svc.add_task(1, "d") == "46d"
    label, resumed, _ = svc.start_tree(1, "oklang rewrite")  # resume by name
    assert resumed is True
    assert svc.add_task(1, "e") == "46e"
    shutil.rmtree(work, ignore_errors=True)


@check("stable numbers across removals")
def _():
    svc, work = _fresh_service()
    svc.create_list(1, True, "list")
    for ch in "abcd":
        svc.add_task(1, ch)
    svc.remove_task(1, "2")
    assert svc.add_task(1, "e") == "5"
    labels = [f"{t.number}{t.branch}" for t in svc.containers[1].todo["list"].todo]
    assert labels == ["1", "3", "4", "5"], labels
    shutil.rmtree(work, ignore_errors=True)


@check("branches past z, subtree range removal")
def _():
    svc, work = _fresh_service()
    svc.create_list(1, True, "list")
    svc.start_tree(1, "root")  # 1
    for i in range(28):
        svc.add_task(1, f"b{i}")
    branches = [t.branch for t in svc.containers[1].todo["list"].todo if t.branch]
    assert branches[-1] == "ab", branches[-1]
    svc.stop_tree(1)
    assert svc.add_task(1, "next") == "2"
    assert svc.remove_tasks(1, 1, 2) == 30  # root + 28 branches + next
    shutil.rmtree(work, ignore_errors=True)


@check("metadata stamps + task_info")
def _():
    svc, work = _fresh_service()
    svc.create_list(1, True, "list")
    before = time.time()
    svc.add_task(1, "original")
    info = svc.task_info(1, 1, "", "1")
    assert info["created_at"] >= before and info["edited_at"] is None
    svc.update_task(1, "1", "edited", priority="high")
    info = svc.task_info(1, 1, "", "1")
    assert info["edited_count"] == 1 and info["edited_at"] >= before
    assert info["priority"] == "high"
    shutil.rmtree(work, ignore_errors=True)


@check("legacy payload loads with numbers + creation backfill")
def _():
    svc, work = _fresh_service()
    svc.create_list(1, True, "list")
    ms = 1790000000000
    from leech.cogs.todo.service import TodoTask

    lst = svc.containers[1].todo["list"]
    for i in range(3):
        lst.todo.append(TodoTask(content=f"old {i}", id=ms + i))
    svc._persist(1)

    from leech.core.store import get_store

    raw = get_store().get("todo", "1")
    assert raw["todo"]["list"]["todo"][0].get("number") is None  # raw is legacy-shaped
    reset = __import__(
        "leech.core.store", fromlist=["reset_store"]
    ).reset_store
    from leech.cogs.todo.service import TodoService

    reset()
    svc2 = TodoService()
    lst2 = svc2.containers[1].todo["list"]
    assert [t.number for t in lst2.todo] == [1, 2, 3]
    assert lst2.seq == 4
    assert all(t.created_at is not None for t in lst2.todo)
    assert svc2.add_task(1, "new") == "4"
    shutil.rmtree(work, ignore_errors=True)


@check("armed root removed -> adds fall back to numbers")
def _():
    svc, work = _fresh_service()
    svc.create_list(1, True, "list")
    label, _, _ = svc.start_tree(1, "ghost")
    svc.remove_task(1, label)
    assert svc.add_task(1, "fallback").isdigit()
    assert svc.containers[1].todo["list"].tree_root is None
    shutil.rmtree(work, ignore_errors=True)


@check("jsonb-style key shuffle survives (v10.9 invariant intact)")
def _():
    svc, work = _fresh_service()
    svc.create_list(1, True, "list")
    svc.add_task(1, "task")
    svc.start_tree(1, "tree")
    svc.add_task(1, "branch")

    from leech.core.store import get_store

    raw = get_store().get("todo", "1")
    mangled = sorted(raw["todo"].keys(), key=lambda k: (len(k), k.encode()))
    raw["todo"] = {k: raw["todo"][k] for k in mangled}
    get_store().put("todo", "1", raw)

    from leech.core.store import reset_store
    from leech.cogs.todo.service import TodoService

    reset_store()
    svc2 = TodoService()
    lst = svc2.containers[1].todo["list"]
    assert lst.tree_root == 2  # armed state survived the round-trip
    assert svc2.add_task(1, "more") == "2b"
    shutil.rmtree(work, ignore_errors=True)


@check("repo test suite")
def _():
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": os.path.join(REPO, "src"),
            "BOT_ID": "1",
            "OWNER_ID": "1",
            "AI_CHAIN": "",
            "LEECH_STORE": "json",
            "LEECH_STORE_DIR": tempfile.mkdtemp(prefix="verify46-pytest-"),
        }
    )
    out = subprocess.run(
        [sys.executable, "-m", "pytest", os.path.join(REPO, "tests"), "-q"],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    tail = (out.stdout or "").strip().splitlines()[-1]
    assert out.returncode == 0, tail
    print(f"    pytest: {tail}")


def main():
    os.environ.setdefault("BOT_ID", "1")
    os.environ.setdefault("OWNER_ID", "1")
    os.environ.setdefault("AI_CHAIN", "")
    sys.path.insert(0, os.path.join(REPO, "src"))
    import leech.leech  # noqa: F401  (import order matters, see task 43)

    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"battery RED: {failures} check(s) failed")
        raise SystemExit(1)
    print(f"battery GREEN: {len(CHECKS)} checks")


if __name__ == "__main__":
    main()
