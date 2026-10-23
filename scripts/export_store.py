#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

KNOWN_NAMESPACES = (
    "todo",
    "todo_undo",
    "todo_backup",
    "user_prefs",
    "security_grants",
    "security_delegations",
    "ai_personas",
    "ai_config",
    "command_templates",
    "mono_users",
    "lrc_config",
    "ytdlp_prefs",
    "channel",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="copy the current store's kv state into a json store dir."
    )
    parser.add_argument(
        "--to",
        default=None,
        help="target json store directory (default: LEECH_STORE_DIR env, "
        "else persistent/v2). created if missing.",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

    backend = (os.getenv("LEECH_STORE") or "json").strip().lower()
    if backend not in ("json", "supabase"):
        print(f"unknown LEECH_STORE backend '{backend}' (json, supabase).")
        return 2
    if backend == "supabase" and not (
        os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY")
    ):
        print("supabase source needs SUPABASE_URL + SUPABASE_SERVICE_KEY.")
        return 2

    from leech.core.store import get_store

    source = get_store(sync=True)
    print(f"source backend: {backend}")

    snapshot: dict[str, dict] = {}
    mirror = getattr(source, "_mirror", None)  # supabase mirror carries ns
    namespaces = sorted(set(KNOWN_NAMESPACES) | set(mirror or {}))
    for ns in namespaces:
        try:
            rows = source.items(ns)
        except Exception as exc:
            print(f"  ! skipping namespace '{ns}': {exc}")
            continue
        if rows:
            snapshot[ns] = rows

    total = sum(len(rows) for rows in snapshot.values())
    if not total:
        print("source store held nothing - nothing to export.")
        return 1
    for ns in sorted(snapshot):
        print(f"  {ns:<20} {len(snapshot[ns]):>5} row(s)")
    print(f"  {'TOTAL':<20} {total:>5}")

    from leech.core.store import reset_store

    os.environ["LEECH_STORE"] = "json"
    if args.to:
        os.environ["LEECH_STORE_DIR"] = args.to
    elif "LEECH_STORE_DIR" not in os.environ:
        os.environ["LEECH_STORE_DIR"] = "persistent/v2"
    reset_store()
    target = get_store(sync=True)
    print(f"target json store: {target.root}")

    written = 0
    for ns, rows in snapshot.items():
        for key, value in rows.items():
            target.put(ns, key, value)
            written += 1
    print(
        f"wrote {written} row(s). rollback-ready: point LEECH_STORE at "
        f"json (or just unset it) and this directory is the live state."
    )

    problems = 0
    for ns, rows in snapshot.items():
        live = target.items(ns)
        for key, value in rows.items():
            if live.get(key) != value:
                problems += 1
                print(f"  ! read-back mismatch: {ns}/{key}")
    if problems:
        print(f"READ-BACK PROBLEMS: {problems}")
        return 3
    print("read-back clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
