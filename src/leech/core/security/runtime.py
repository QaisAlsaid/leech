# leech/core/security/runtime.py
from __future__ import annotations

import logging
from typing import Optional

from leech.core.store import Store, get_store, StoreError

from .model import Decision, Grant

AI_FORBIDDEN = ("command:auth.", "command:sudo")

# v11: security rides the store. rows match the migrator's shapes:
#   security_grants      ns: {actor: [{scope, allow}, ...]}
#   security_delegations ns: {actor: {scope: bool}}
NS_GRANTS = "security_grants"
NS_DELEGATIONS = "security_delegations"

_logger = logging.getLogger(__name__)


class Security:
    def __init__(
        self,
        owner_ids: list[int],
        store: Store | None = None,
    ):
        self._owners = set(owner_ids)
        self._store = store or get_store()
        self.grants: list[Grant] = []
        self.delegations: dict[str, dict[str, bool]] = {}
        # v11: set when the store failed to load. reads run on defaults,
        # writes are refused - a failed load must never turn into a wipe.
        self._broken = False
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
        try:
            grant_rows = self._store.items(NS_GRANTS)
            deleg_rows = self._store.items(NS_DELEGATIONS)
        except StoreError as e:
            self._broken = True
            _logger.error(
                "security store unreadable (%s). starting with owners "
                "only; changes will NOT be saved this session - fix the "
                "store, then restart.",
                e,
            )
            return
        for actor, rows in grant_rows.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and row.get("scope") is not None:
                    self.grants.append(
                        Grant(
                            actor=str(actor),
                            scope=str(row["scope"]),
                            allow=bool(row.get("allow", True)),
                        )
                    )
        for actor, scopes in deleg_rows.items():
            if isinstance(scopes, dict):
                self.delegations[str(actor)] = {
                    str(scope): bool(value) for scope, value in scopes.items()
                }

    def _persist(self) -> None:
        if self._broken:
            _logger.error(
                "security change NOT saved: the store failed to load this "
                "session. restart once the store is reachable; nothing was "
                "overwritten."
            )
            return
        try:
            grants: dict[str, list[dict]] = {}
            for grant in self.grants:
                grants.setdefault(grant.actor, []).append(
                    {"scope": grant.scope, "allow": grant.allow}
                )
            live = self._store.items(NS_GRANTS)
            for actor, rows in grants.items():
                self._store.put(NS_GRANTS, actor, rows)
            for actor in live:
                if actor not in grants:
                    self._store.delete(NS_GRANTS, actor)

            live = self._store.items(NS_DELEGATIONS)
            for actor, scopes in self.delegations.items():
                self._store.put(NS_DELEGATIONS, actor, scopes)
            for actor in live:
                if actor not in self.delegations:
                    self._store.delete(NS_DELEGATIONS, actor)
        except StoreError as e:
            _logger.error("security change not saved: %s", e)
