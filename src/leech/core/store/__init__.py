"""process-wide store access. `get_store()` once, env decides the rest."""

from __future__ import annotations

import logging

from .base import Store, StoreError, store_env
from .json_store import JsonStore

_logger = logging.getLogger(__name__)

_instances: dict[bool, Store] = {}


def get_store(*, sync: bool = False) -> Store:
    """the shared store instance. `sync=True` gives threaded callers
    (the supervisor) a separate instance whose writes block instead of
    riding the async flusher."""
    cached = _instances.get(sync)
    if cached is not None:
        return cached

    backend, url, key = store_env()
    if backend in ("supabase", "postgres", "rest"):
        from .supabase import SupabaseStore

        store = SupabaseStore(url, key, sync=sync)
        _logger.info(f"store backend: supabase ({'sync' if sync else 'async'})")
    elif backend in ("", "json", "file"):
        store = JsonStore()
        _logger.info(f"store backend: json ({store.root})")
    else:
        raise StoreError(
            f"unknown LEECH_STORE backend: '{backend}' (known: json, supabase)."
        )
    _instances[sync] = store
    return store


def reset_store() -> None:
    """tests only: drop cached instances so env changes take effect."""
    for store in _instances.values():
        try:
            store.close()
        except Exception:
            _logger.exception("closing store during reset failed")
    _instances.clear()
