from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .base import Store, StoreError, sanitize_ns


class JsonStore(Store):
    def __init__(self, root: str | Path | None = None):
        env_dir = (os.getenv("LEECH_STORE_DIR") or "").strip()
        self.root = Path(root or env_dir or "persistent/v2")
        self.root.mkdir(parents=True, exist_ok=True)

    def _file(self, ns: str) -> Path:
        return self.root / f"{sanitize_ns(ns)}.json"

    def _log_file(self, ns: str) -> Path:
        return self.root / f"{sanitize_ns(ns)}.jsonl"

    def _read_ns(self, ns: str) -> dict:
        path = self._file(ns)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise StoreError(f"store namespace '{ns}' is unreadable: {e}")
        return data if isinstance(data, dict) else {}

    def _write_ns(self, ns: str, data: dict) -> None:
        path = self._file(ns)
        tmp = path.with_suffix(".json.temp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            raise StoreError(f"failed to persist namespace '{ns}': {e}") from e

    def get(self, ns: str, key: str) -> Any | None:
        return self._read_ns(ns).get(key)

    def put(self, ns: str, key: str, value: Any) -> None:
        data = self._read_ns(ns)
        data[key] = value
        self._write_ns(ns, data)

    def delete(self, ns: str, key: str) -> None:
        data = self._read_ns(ns)
        if key in data:
            data.pop(key)
            self._write_ns(ns, data)

    def items(self, ns: str) -> dict[str, Any]:
        return dict(self._read_ns(ns))

    def append_log(self, ns: str, record: dict) -> None:
        path = self._log_file(ns)
        try:
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as e:
            raise StoreError(f"failed to append to log '{ns}': {e}") from e

    def close(self) -> None:
        pass
