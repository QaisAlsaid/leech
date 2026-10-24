from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import threading
from typing import Any

import httpx

from .base import Store, StoreError

_logger = logging.getLogger(__name__)

FLUSH_EVERY = 0.5
QUEUE_CAP = 1024
BATCH_ROWS = 256
RETRY_BACKOFF = 1.0

_seq = itertools.count(1)


class _Op:
    __slots__ = ("kind", "ns", "key", "value", "seq")

    def __init__(self, kind: str, ns: str, key: str, value: Any = None):
        self.kind = kind  # put | delete | log
        self.ns = ns
        self.key = key
        self.value = value
        self.seq = next(_seq)


class SupabaseStore(Store):
    def __init__(self, url: str, key: str, *, sync: bool = False):
        if not url or not key:
            raise StoreError(
                "LEECH_STORE=supabase needs SUPABASE_URL and "
                "SUPABASE_SERVICE_KEY (or SUPABASE_KEY)."
            )
        self._url = url
        self._key = key
        self._sync = sync
        self._mirror: dict[str, dict[str, Any]] = {}
        self._mirror_lock = threading.Lock()
        self._queue: list[_Op] = []
        self._queue_lock = threading.Lock()
        self._flusher: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    def _headers(self, *, write: bool) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "authorization": f"Bearer {self._key}",
        }
        if write:
            headers["content-type"] = "application/json"
            headers["prefer"] = "resolution=merge-duplicates"
        return headers

    def _kv_url(self, query: str) -> str:
        return f"{self._url}/rest/v1/leech_kv{query}"

    def _check(self, response: httpx.Response, what: str) -> None:
        if response.status_code >= 300:
            detail = f" details: {response.text[:300]}" if response.text else ""
            raise StoreError(
                f"supabase rejected request ({what}): "
                f"{response.status_code}{detail}"
            )

    def _load_ns(self, ns: str) -> None:
        try:
            with httpx.Client(timeout=15.0) as http:
                response = http.get(
                    self._kv_url(f"?ns=eq.{ns}&select=k,v"),
                    headers=self._headers(write=False),
                )
                self._check(response, f"load namespace '{ns}'")
        except httpx.HTTPError as e:
            raise StoreError(f"failed to load namespace '{ns}': {e}") from e
        rows = response.json()
        with self._mirror_lock:
            self._mirror[ns] = {str(row["k"]): row["v"] for row in rows if "k" in row}

    def _ensure_loaded(self, ns: str) -> dict[str, Any]:
        with self._mirror_lock:
            loaded = ns in self._mirror
            snapshot = dict(self._mirror.get(ns, {}))
        if not loaded:
            self._load_ns(ns)
            with self._mirror_lock:
                snapshot = dict(self._mirror.get(ns, {}))
        return snapshot

    def get(self, ns: str, key: str) -> Any | None:
        return self._ensure_loaded(ns).get(key)

    def items(self, ns: str) -> dict[str, Any]:
        return self._ensure_loaded(ns)

    def put(self, ns: str, key: str, value: Any) -> None:
        try:
            json.dumps(value)
        except (TypeError, ValueError) as e:
            raise StoreError(f"value for {ns}/{key} is not storable: {e}")
        with self._mirror_lock:
            self._mirror.setdefault(ns, {})[key] = value
        if self._sync:
            self._send_sync_ops([_Op("put", ns, key, value)])
        else:
            self._enqueue(_Op("put", ns, key, value))

    def delete(self, ns: str, key: str) -> None:
        with self._mirror_lock:
            self._mirror.setdefault(ns, {}).pop(key, None)
        if self._sync:
            self._send_sync_ops([_Op("delete", ns, key)])
        else:
            self._enqueue(_Op("delete", ns, key))

    def append_log(self, ns: str, record: dict) -> None:
        try:
            json.dumps(record)
        except (TypeError, ValueError) as e:
            raise StoreError(f"log record for '{ns}' is not storable: {e}")
        if self._sync:
            self._send_sync_ops([_Op("log", ns, "", record)])
        else:
            self._enqueue(_Op("log", ns, "", record))

    def _enqueue(self, op: _Op) -> None:
        with self._queue_lock:
            self._queue.append(op)
            if len(self._queue) > QUEUE_CAP:
                dropped = self._queue.pop(0)
                _logger.error(
                    "store queue overflow - dropping oldest op "
                    f"{dropped.kind} {dropped.ns}/{dropped.key}"
                )
        self._spawn_flusher()

    def _spawn_flusher(self) -> None:
        if self._sync or self._closed or self._flusher is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flusher = loop.create_task(self._drain_forever(), name="store-flusher")

    async def _drain_forever(self) -> None:
        while not self._closed:
            await asyncio.sleep(FLUSH_EVERY)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("store flusher cycle failed; retrying")

    def _send_sync_ops(self, ops: list[_Op]) -> None:
        with httpx.Client(timeout=20.0) as http:
            for op in ops:
                if op.kind == "put":
                    response = http.post(
                        self._kv_url("?on_conflict=ns,k"),
                        headers=self._headers(write=True),
                        json=[{"ns": op.ns, "k": op.key, "v": op.value}],
                    )
                    self._check(response, f"put {op.ns}/{op.key}")
                elif op.kind == "delete":
                    response = http.request(
                        "DELETE",
                        self._kv_url(f"?ns=eq.{op.ns}&k=eq.{op.key}"),
                        headers=self._headers(write=False),
                    )
                    self._check(response, f"delete {op.ns}/{op.key}")
                else:
                    response = http.post(
                        f"{self._url}/rest/v1/leech_log",
                        headers=self._headers(write=True),
                        json=[{"ns": op.ns, "v": op.value}],
                    )
                    self._check(response, f"log {op.ns}")

    def _coalesce(self, ops: list[_Op]) -> list[_Op]:
        latest: dict[tuple[str, str], _Op] = {}
        logs: list[_Op] = []
        for op in ops:
            if op.kind == "log":
                logs.append(op)
            else:
                latest[(op.ns, op.key)] = op
        return sorted(latest.values(), key=lambda op: op.seq) + sorted(
            logs, key=lambda op: op.seq
        )

    async def flush(self, timeout: float = 30.0) -> None:
        if self._sync:
            return
        with self._queue_lock:
            pending = self._coalesce(self._queue)
            self._queue.clear()
        if not pending:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        try:
            await asyncio.wait_for(
                self._flush_async(self._client, pending), timeout=timeout
            )
        except Exception:
            with self._queue_lock:
                self._queue.extend(pending)
                self._queue.sort(key=lambda op: op.seq)
            raise

    async def _flush_async(self, client: httpx.AsyncClient, ops: list[_Op]) -> None:
        for attempt in (1, 2):
            try:
                puts = [op for op in ops if op.kind == "put"]
                for start in range(0, len(puts), BATCH_ROWS):
                    chunk = puts[start : start + BATCH_ROWS]
                    body = [{"ns": op.ns, "k": op.key, "v": op.value} for op in chunk]
                    response = await client.post(
                        self._kv_url("?on_conflict=ns,k"),
                        headers=self._headers(write=True),
                        json=body,
                    )
                    self._check(response, f"upsert {len(chunk)} row(s)")
                for op in (op for op in ops if op.kind == "delete"):
                    response = await client.request(
                        "DELETE",
                        self._kv_url(f"?ns=eq.{op.ns}&k=eq.{op.key}"),
                        headers=self._headers(write=False),
                    )
                    self._check(response, f"delete {op.ns}/{op.key}")
                logs = [op for op in ops if op.kind == "log"]
                if logs:
                    body = [{"ns": op.ns, "v": op.value} for op in logs]
                    response = await client.post(
                        f"{self._url}/rest/v1/leech_log",
                        headers=self._headers(write=True),
                        json=body,
                    )
                    self._check(response, f"log {len(logs)} row(s)")
                return
            except StoreError:
                if attempt == 2:
                    raise
                await asyncio.sleep(RETRY_BACKOFF)

    async def aclose(self) -> None:
        self._closed = True
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
            self._flusher = None
        try:
            await self.flush(timeout=10.0)
        except Exception:
            _logger.exception("final store flush failed - pending writes lost")
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def close(self) -> None:
        if self._sync:
            self._closed = True
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and not loop.is_closed():
            loop.create_task(self.aclose())
        self._closed = True
