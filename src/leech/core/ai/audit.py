from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class ActionLog:
    def __init__(self, path: Path = Path("persistent/v1/ai/actions.jsonl")):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        kind: str,
        principal: int,
        command: str | None = None,
        arguments: dict[str, Any] | None = None,
        outcome: str = "ok",
        note: str = "",
    ) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "kind": kind,  # run|denied|error|dry
            "principal": principal,
            "via": "ai",
            "command": command,
            "arguments": arguments,
            "outcome": outcome,
            "note": note,
        }
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, separators=(",", ":")) + "\n")
