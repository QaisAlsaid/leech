# leech/core/security/runtime.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .model import Decision, Grant

AI_FORBIDDEN = ("command:auth.", "command:sudo")


class Security:
    def __init__(
        self,
        owner_ids: list[int],
        store: Path = Path("persistent/v1/security/security.json"),
    ):
        self._owners = set(owner_ids)
        self._store = store
        self.grants: list[Grant] = []
        self.delegations: dict[str, dict[str, bool]] = {}
        self._load()

    # -- decisions ------------------------------------------------------------
    def decide_user(self, user_id: int, scope: str) -> Decision:
        if user_id in self._owners:
            return Decision(True, "owner")
        best = self._best_grant(f"user:{user_id}", scope)
        if best is None:
            return Decision(
                False,
                f"you don't have access to '{scope}' (ask the owner for a grant 😁).",
            )
        if not best.allow:
            return Decision(False, f"'{scope}' is denied for you 😁.")
        return Decision(True, "granted")

    def decide_ai(self, user_id: int, scope: str) -> Decision:
        base = self.decide_user(user_id, scope)
        if not base.allowed:
            return base
        if scope == "command:sudo" or scope.startswith(AI_FORBIDDEN):
            return Decision(False, "the assistant can't touch permissions or sudo.")
        delegated = self._delegation(
            f"user:{user_id}", scope
        )  # rule 3 (v10.9): per-user, default ASK - the ai cog confirms
        # with the user before running; only an explicit allow skips the
        # prompt. dispatch-level this still counts as allowed.
        if delegated is False:
            return Decision(
                False,
                f"you haven't allowed the assistant to run '{scope}' (see `>ai perms` 😁).",
            )
        return base

    def delegation_mode(self, user_id: int, scope: str) -> str:
        """v10.9 AI_OPSEC_4: 'allow' | 'deny' | 'ask' for one scope.
        missing entry = ask (the new default)."""
        value = self._delegation(f"user:{user_id}", scope)
        if value is None:
            return "ask"
        return "allow" if value else "deny"

    # -- grants (owner-managed) --------------------------------------------------
    def set_grant(self, user_id: int, scope: str, allow: bool) -> None:
        actor = f"user:{user_id}"
        self.grants = [
            g for g in self.grants if not (g.actor == actor and g.scope == scope)
        ]
        self.grants.append(Grant(actor=actor, scope=scope, allow=allow))
        self._persist()

    def clear_grant(self, user_id: int, scope: str) -> None:
        actor = f"user:{user_id}"
        self.grants = [
            g for g in self.grants if not (g.actor == actor and g.scope == scope)
        ]
        self._persist()

    def grants_of(self, user_id: int) -> list[Grant]:
        return [g for g in self.grants if g.actor == f"user:{user_id}"]

    # -- delegation (per-user, default ASK since v10.9) -----------------------------
    def deny_delegation(self, user_id: int, scope: str) -> None:
        self.delegations.setdefault(f"user:{user_id}", {})[scope] = False
        self._persist()

    def allow_delegation(self, user_id: int, scope: str) -> None:
        # v10.9: allow is now an explicit stored state (True), not "remove
        # the deny" - the default flipped from allow to ask.
        self.delegations.setdefault(f"user:{user_id}", {})[scope] = True
        self._persist()

    def ask_delegation(self, user_id: int, scope: str) -> None:
        """back to the v10.9 default: confirm with the user each time."""
        entries = self.delegations.get(f"user:{user_id}", {})
        entries.pop(scope, None)
        if not entries:
            self.delegations.pop(f"user:{user_id}", None)
        self._persist()

    def delegations_of(self, user_id: int) -> dict[str, bool]:
        return dict(self.delegations.get(f"user:{user_id}", {}))

    # -- matching --------------------------------------------------------------------
    def _best_grant(self, actor: str, scope: str) -> Optional[Grant]:
        hits = [
            g
            for g in self.grants
            if g.actor in ("*", actor) and self._scope_match(g.scope, scope)
        ]
        if not hits:
            return None
        return min(
            hits, key=lambda g: (len(g.scope), g.allow)
        )  # specific wins; deny on ties

    def _delegation(self, actor: str, scope: str) -> Optional[bool]:
        entries = self.delegations.get(actor, {})
        hits = [p for p in entries if self._scope_match(p, scope)]
        return entries[max(hits, key=len)] if hits else None

    @staticmethod
    def _scope_match(pattern: str, scope: str) -> bool:
        if pattern in ("*", scope):
            return True
        if pattern.endswith(".*"):
            return scope.startswith(pattern[:-1])
        return False

    # -- persistence --------------------------------------------------------------
    def _load(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        if not self._store.exists():
            return
        data = json.loads(self._store.read_text(encoding="utf-8"))
        self.grants = [Grant(**g) for g in data.get("grants", [])]
        self.delegations = data.get("delegations", {})

    def _persist(self) -> None:
        tmp = self._store.with_suffix(".json.temp")
        tmp.write_text(
            json.dumps(
                {
                    "grants": [g.model_dump() for g in self.grants],
                    "delegations": self.delegations,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._store)
