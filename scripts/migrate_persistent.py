#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

DOC_NAMESPACES = ("ai_config", "lrc_config", "channel")


def read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"unreadable v1 file {path}: {e}")


def collect(v1: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict]]:
    kv: dict[str, dict[str, Any]] = {}
    docs: dict[str, Any] = {}
    logs: list[dict] = []

    todo = read_json(v1 / "todo" / "todo.json")
    if isinstance(todo, dict):
        kv["todo"] = {str(user): data for user, data in todo.items()}

    undo: dict[str, Any] = {}
    undoroot = v1 / "todo" / "undo"
    if undoroot.is_dir():
        for entry in sorted(undoroot.glob("*/*.json")):
            undo[f"{entry.parent.name}/{entry.stem}"] = read_json(entry)
    if undo:
        kv["todo_undo"] = undo

    backups: dict[str, Any] = {}
    backuproot = v1 / "todo" / "backups"
    if backuproot.is_dir():
        for entry in sorted(backuproot.glob("*.json")):
            backups[entry.stem] = {"containers": read_json(entry)}
    if backups:
        kv["todo_backup"] = backups

    security = read_json(v1 / "security" / "security.json")
    if isinstance(security, dict):
        grants: dict[str, list[dict]] = {}
        for grant in security.get("grants") or []:
            if not isinstance(grant, dict) or "actor" not in grant:
                continue
            grants.setdefault(str(grant["actor"]), []).append(
                {"scope": grant.get("scope"), "allow": grant.get("allow")}
            )
        if grants:
            kv["security_grants"] = grants
        delegations = security.get("delegations") or {}
        if isinstance(delegations, dict) and delegations:
            kv["security_delegations"] = {
                str(actor): scopes for actor, scopes in delegations.items()
            }

    ai = read_json(v1 / "ai" / "ai.json")
    if isinstance(ai, dict):
        personas = ai.get("personas") or {}
        if isinstance(personas, dict) and personas:
            kv["ai_personas"] = {str(cid): text for cid, text in personas.items()}
        config = {
            key: ai[key]
            for key in ("active", "muted", "window_budget", "chain")
            if key in ai
        }
        if config:
            docs["ai_config"] = config

    actions = v1 / "ai" / "actions.jsonl"
    if actions.is_file():
        buffer = ""
        for line in actions.read_text(encoding="utf-8").splitlines():
            if not line.strip() and not buffer:
                continue
            buffer = f"{buffer}\n{line}" if buffer else line
            try:
                record = json.loads(buffer, strict=False)
            except json.JSONDecodeError:
                if len(buffer) > 100_000:
                    print(
                        f"  ! dropping an unparseable actions.jsonl chunk: {buffer[:60]}"
                    )
                    buffer = ""
                continue
            logs.append(record)
            buffer = ""
        if buffer:
            print(f"  ! trailing unparseable actions.jsonl chunk: {buffer[:60]}")

    templates = read_json(v1 / "commands" / "templates.json")
    if isinstance(templates, dict):
        rows = templates.get("templates") or []
        if isinstance(rows, list) and rows:
            kv["command_templates"] = {
                str(t["name"]): t for t in rows if isinstance(t, dict) and "name" in t
            }

    mono = read_json(v1 / "monochrome" / "monochrome.json")
    if isinstance(mono, dict):
        users = mono.get("users") or {}
        if isinstance(users, dict) and users:
            kv["mono_users"] = {str(user): prefs for user, prefs in users.items()}

    lrc = read_json(v1 / "lrc" / "lrc.json")
    if lrc is not None:
        docs["lrc_config"] = lrc

    channel = read_json(v1 / "channel.json")
    if channel is not None:
        docs["channel"] = channel

    return kv, docs, logs


def make_store(backend: str):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from leech.core.store import Store, reset_store

    os.environ["LEECH_STORE"] = backend
    if backend == "json" and "LEECH_STORE_DIR" not in os.environ:
        os.environ["LEECH_STORE_DIR"] = "persistent/v2"
    reset_store()
    from leech.core.store import get_store

    return get_store(sync=True)  # blocking writes: no flusher thread


def wipe(store, kv: dict, docs: dict) -> int:
    removed = 0
    for ns in list(kv) + list(docs):
        for key in store.items(ns):
            store.delete(ns, key)
            removed += 1
    return removed


def write(store, kv: dict, docs: dict, logs: list[dict]) -> int:
    written = 0
    for ns, rows in kv.items():
        for key, value in rows.items():
            store.put(ns, key, value)
            written += 1
    for ns, value in docs.items():
        store.put_doc(ns, value)
        written += 1
    for record in logs:
        store.append_log("ai_log", record)
        written += 1
    return written


def verify(store, kv: dict, docs: dict, logs: list[dict]) -> list[str]:
    problems = []
    for ns, rows in kv.items():
        live = store.items(ns)
        for key, value in rows.items():
            if live.get(key) != value:
                problems.append(f"{ns}/{key}: read-back mismatch")
    for ns, value in docs.items():
        if store.get_doc(ns) != value:
            problems.append(f"{ns}: doc read-back mismatch")
    if logs:
        print(f"  . {len(logs)} ai_log rows written (insert-only, not re-read)")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description="migrate persistent/v1 into the v10.7 store."
    )
    parser.add_argument(
        "--from",
        dest="source",
        default="persistent/v1",
        help="v1 tree to read (default: persistent/v1)",
    )
    parser.add_argument(
        "--backend",
        choices=("json", "supabase"),
        default=None,
        help="store backend (default: LEECH_STORE env, else json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and report only - write nothing",
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="delete existing keys in target namespaces before writing",
    )
    args = parser.parse_args()

    v1 = Path(args.source)
    if not v1.is_dir():
        print(f"no v1 tree at {v1} - nothing to migrate.")
        return 1

    kv, docs, logs = collect(v1)

    total = sum(len(rows) for rows in kv.values()) + len(docs) + len(logs)
    if not total:
        print(f"{v1} held nothing migratable. done.")
        return 0

    print(f"collected from {v1}:")
    for ns, rows in sorted(kv.items()):
        print(f"  {ns:<20} {len(rows):>5} row(s)")
    for ns, value in sorted(docs.items()):
        print(f"  {ns:<20} {'doc':>5}")
    if logs:
        print(f"  {'ai_log':<20} {len(logs):>5} row(s)")
    print(f"  {'TOTAL':<20} {total:>5}")

    if args.dry_run:
        print("dry run - nothing written.")
        return 0

    backend = args.backend or (os.getenv("LEECH_STORE") or "json").strip().lower()
    if backend not in ("json", "supabase"):
        print(f"unknown backend '{backend}' (known: json, supabase).")
        return 2

    if backend == "supabase" and not (
        os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY")
    ):
        print("supabase backend needs SUPABASE_URL + SUPABASE_SERVICE_KEY.")
        print("(and deploy/supabase.sql must have been run once.)")
        return 2

    store = make_store(backend)

    if args.wipe:
        removed = wipe(store, kv, docs)
        print(f"wiped {removed} stale key(s) from the target.")

    written = write(store, kv, docs, logs)
    print(f"wrote {written} row(s) into the {backend} store.")

    problems = verify(store, kv, docs, logs)
    if problems:
        print("READ-BACK PROBLEMS:")
        for line in problems:
            print(f"  ! {line}")
        return 3
    print("read-back clean - every row matches what was collected.")
    print("v1 files were left untouched; remove them when you trust the new store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
