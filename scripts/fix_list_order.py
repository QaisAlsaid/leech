#!/usr/bin/env python3
"""one-shot repair: restore todo list ORDER in the live store after the
supabase migration mangled it.

what happened (v10.7-v10.8 era): TodoContainer.todo is a JSON *object*
keyed by list name. postgres jsonb does not preserve object key order
(keys come back sorted by length, then bytewise), so every container
that round-tripped through supabase came back with its lists shuffled -
which also silently remapped `>todo list 2`-style numeric indices.

v10.9 fixes the going-forward part (TodoContainer.list_order - an
array, which jsonb preserves). this tool fixes the already-mangled
rows: it reads the ORIGINAL order from the untouched v1/v2 files on
disk, reorders each live container to match (lists created after the
migration append at the end, deleted ones drop), stamps list_order,
and writes the rows back.

safe by construction:
- tasks, editors, active list, priorities, dues... are taken from the
  LIVE row as-is (the DB wins on content; only order is repaired).
- v1/v2 files are only read, never written.
- --dry-run shows the plan without writing anything.

run it with the bot STOPPED (a running bot would overwrite the fix
from its in-memory mirror on the next edit), then start the bot:

    python scripts/fix_list_order.py --dry-run
    python scripts/fix_list_order.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! unreadable {path}: {e}")
        return None


def file_orders(v1: Path, v2: Path) -> dict[str, list[str]]:
    """user_id -> original list order, from disk. v2 (the json store the
    bot itself wrote while on v10.7) wins over v1 (the pre-v10.7 tree):
    it is the most recent on-disk snapshot of the intended order."""
    orders: dict[str, list[str]] = {}

    todo_v1 = read_json(v1 / "todo" / "todo.json")
    if isinstance(todo_v1, dict):
        for user, data in todo_v1.items():
            todo = data.get("todo") if isinstance(data, dict) else None
            if isinstance(todo, dict):
                orders[str(user)] = list(todo.keys())

    todo_v2 = read_json(v2 / "todo.json")
    if isinstance(todo_v2, dict):
        for user, data in todo_v2.items():
            todo = data.get("todo") if isinstance(data, dict) else None
            if isinstance(todo, dict):
                orders[str(user)] = list(todo.keys())

    return orders


def main() -> int:
    parser = argparse.ArgumentParser(
        description="repair todo list order in the live store (v10.9)."
    )
    parser.add_argument(
        "--backend", choices=("json", "supabase"), default=None,
        help="live store backend (default: LEECH_STORE env, else json).",
    )
    parser.add_argument(
        "--from", dest="source", default="persistent/v1",
        help="the untouched v1 tree (default: persistent/v1)",
    )
    parser.add_argument(
        "--v2-from", default=None,
        help="where the v10.7 json store lives (default: LEECH_STORE_DIR "
        "env, else persistent/v2)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    v1 = Path(args.source)
    v2 = Path(args.v2_from or os.getenv("LEECH_STORE_DIR") or "persistent/v2")
    orders = file_orders(v1, v2)
    if not orders:
        print(f"no order info found in {v1} or {v2} - nothing to repair from.")
        return 1

    backend = args.backend or (os.getenv("LEECH_STORE") or "json").strip().lower()
    if backend not in ("json", "supabase"):
        print(f"unknown backend '{backend}' (known: json, supabase).")
        return 2
    if backend == "supabase" and not (
        os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY")
    ):
        print("supabase backend needs SUPABASE_URL + SUPABASE_SERVICE_KEY.")
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    os.environ["LEECH_STORE"] = backend
    from leech.core.store import reset_store, get_store

    reset_store()
    store = get_store(sync=True)

    live = store.items("todo")
    if not live:
        print("the live 'todo' namespace is empty - nothing to repair.")
        return 0

    print(f"repairing list order on the {backend} store "
          f"({len(live)} user row(s)), source order from:")
    print(f"  v1: {v1}{' (missing)' if not v1.is_dir() else ''}")
    print(f"  v2: {v2}{' (missing)' if not v2.is_dir() else ''}")

    changed = untouched = 0
    for key in sorted(live):
        row = live.get(key)
        if not isinstance(row, dict) or not isinstance(row.get("todo"), dict):
            print(f"  {key}: not a todo container row - skipped")
            continue
        live_keys = list(row["todo"].keys())
        known = orders.get(str(key))
        if known is None:
            print(f"  {key}: no file order on disk - left as-is {live_keys}")
            untouched += 1
            continue

        wanted = [n for n in known if n in row["todo"]]
        wanted += [n for n in live_keys if n not in wanted]
        if wanted == live_keys and row.get("list_order") == wanted:
            print(f"  {key}: already in order")
            untouched += 1
            continue

        row["todo"] = {n: row["todo"][n] for n in wanted}
        row["list_order"] = wanted
        print(f"  {key}:")
        print(f"     was: {live_keys}")
        print(f"     now: {wanted}")
        if not args.dry_run:
            store.put("todo", str(key), row)
        changed += 1

    if args.dry_run:
        print(f"dry run - nothing written ({changed} row(s) would change, "
              f"{untouched} already fine).")
        return 0

    print(f"done: {changed} row(s) repaired, {untouched} already fine.")
    if changed:
        print("restart the bot so it reloads the repaired rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
