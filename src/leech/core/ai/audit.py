from __future__ import annotations

import logging
import time
from typing import Any

from leech.core.store import Store, get_store, StoreError

# v11: the audit trail rides the store's append-only log - json backend
# writes ai_log.jsonl, supabase appends to the leech_log table. same ns
# the migrator imports persistent/v1/ai/actions.jsonl into.
NS_LOG = "ai_log"

_logger = logging.getLogger(__name__)


class ActionLog:
    def __init__(self, store: Store | None = None):
        self._store = store or get_store()

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
            "kind": kind,  # run, denied, error, dry
            "principal": principal,
            "via": "ai",
            "command": command,
            "arguments": arguments,
            "outcome": outcome,
            "note": note,
        }
        try:
            self._store.append_log(NS_LOG, entry)
        except StoreError as e:
            # the audit trail must never take a command down with it.
            _logger.error("ai action not logged: %s", e)
