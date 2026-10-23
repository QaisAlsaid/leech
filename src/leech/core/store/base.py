from __future__ import annotations

import os
from typing import Any

from leech.core.error import BaseError


class StoreError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=False)


DOC_KEY = "_"


class Store:
    def get(self, ns: str, key: str) -> Any | None:
        raise NotImplementedError

    def put(self, ns: str, key: str, value: Any) -> None:
        raise NotImplementedError

    def delete(self, ns: str, key: str) -> None:
        raise NotImplementedError

    def items(self, ns: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_doc(self, ns: str) -> Any | None:
        return self.get(ns, DOC_KEY)

    def put_doc(self, ns: str, value: Any) -> None:
        self.put(ns, DOC_KEY, value)

    def append_log(self, ns: str, record: dict) -> None:
        raise NotImplementedError

    def flush(self) -> Any:
        pass

    def close(self) -> None:
        pass


def sanitize_ns(ns: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in ns)
    if not cleaned:
        raise StoreError("empty namespace.")
    return cleaned


def store_env() -> tuple[str, str, str]:
    backend = (os.getenv("LEECH_STORE") or "json").strip().lower()
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or "").strip()
    return backend, url, key
