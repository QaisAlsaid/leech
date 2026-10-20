from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from leech.core.error import BaseError

_logger = logging.getLogger(__name__)

MONO_BASE = (os.getenv("MONO_BASE") or "https://tracks.monochrome.st").strip().rstrip("/")
SERVER_URL = (
    os.getenv("LEECH_HUB_URL") or ""
).strip().rstrip("/")
SERVER_TOKEN = (
    os.getenv("LEECH_HUB_TOKEN")
    or ""
).strip()

QUALITIES = ("flac", "320", "256", "192", "128", "96", "64", "wav", "m4a",
             "opus", "copy")
LYRICS_MODES = ("off", "embed", "sidecar")
LYRICS_PREFERS = ("synced", "plain")
CONFIG_KEYS = ("quality", "lyrics_mode", "lyrics_prefer")
DEFAULT_CONFIG = {"quality": "flac", "lyrics_mode": "sidecar",
                  "lyrics_prefer": "synced"}


class MonoError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


def quality_label(quality: str) -> str:
    q = (quality or "").strip().lower()
    if q in ("flac", "copy"):
        return "flac"
    if q in ("wav", "m4a", "opus"):
        return q
    return f"mp3 {q}"


def fmt_duration(ms) -> str:
    try:
        seconds = max(0, int(ms) // 1000)
    except (TypeError, ValueError):
        seconds = 0
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _names(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value][:4]
    return []


def parse_search(data: dict) -> dict:
    tracks: list[dict] = []
    releases: list[dict] = []
    artists: list[dict] = []

    for item in data.get("tracks") or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or item.get("trackId") or "").strip()
        if not tid:
            continue
        try:
            duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        tracks.append({
            "id": tid,
            "title": str(item.get("title") or "?"),
            "artists": _names(item.get("artistNames")),
            "artwork": item.get("artwork"),
            "duration": duration,
            "release_id": str(item.get("releaseId") or "") or None,
            "explicit": bool(item.get("explicit")),
        })

    for item in data.get("releases") or []:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or item.get("releaseId") or "").strip()
        if not rid:
            continue
        year = str(item.get("releaseDate") or "")[:4]
        releases.append({
            "id": rid,
            "title": str(item.get("title") or "?"),
            "artists": _names(item.get("artistNames")),
            "year": year if year.isdigit() else "",
            "type": str(item.get("releaseType") or "").lower(),
            "label": item.get("label"),
            "artwork": item.get("artwork"),
        })

    for item in data.get("artists") or []:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or item.get("artistId") or "").strip()
        if not aid:
            continue
        artists.append({
            "id": aid,
            "name": str(item.get("displayName") or item.get("name") or "?"),
            "username": item.get("username"),
            "bio": item.get("bio"),
            "avatar": item.get("avatar"),
        })

    return {"tracks": tracks, "releases": releases, "artists": artists}


class MonoService:
    def __init__(self, *, store: Path = Path("persistent/v1/monochrome/monochrome.json")):
        self._store = store
        self._users: dict[int, dict] = {}
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._load()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def server_configured(self) -> bool:
        return bool(SERVER_URL and SERVER_TOKEN)

    @property
    def server_url(self) -> str:
        return SERVER_URL

    def require_server(self) -> None:
        if not self.server_configured:
            raise MonoError(
                "this needs the leech-hub. set LEECH_HUB_URL / "
                "LEECH_HUB_TOKEN on the bot. (flac track links work without it)."
            )

    def _headers(self) -> dict:
        return {"x-leech-token": SERVER_TOKEN} if SERVER_TOKEN else {}

    async def _api(self, method: str, path: str, body: dict | None = None,
                   params: dict | None = None) -> dict:
        if not self.server_configured:
            raise MonoError(
                "the leech-hub isn't configured (LEECH_HUB_URL / "
                "LEECH_HUB_TOKEN)."
            )
        try:
            if method == "GET":
                resp = await self._http.get(
                    SERVER_URL + path, params=params, headers=self._headers())
            else:
                resp = await self._http.post(
                    SERVER_URL + path, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MonoError(
                f"leech-hub unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 401:
            raise MonoError(
                "the leech-hub rejected the token "
                "(LEECH_HUB_TOKEN vs the hub's TOKEN)."
            )
        if resp.status_code != 200:
            try:
                detail = str(resp.json()["error"])
            except Exception:
                try:
                    detail = str(resp.json()["detail"])
                except Exception:
                    detail = resp.text[:200]
            raise MonoError(f"leech-hub answered {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except Exception as e:
            raise MonoError(f"the leech-hub returned invalid json, details: {e}") from e
        if not data.get("ok"):
            raise MonoError(str(data.get("error") or "unknown hub error"))
        return data

    async def health(self) -> dict:
        return await self._api("GET", "/api/health")

    async def submit_track(self, track: dict, quality: str,
                           lyrics: Optional[dict] = None) -> str:
        data = await self._api("POST", "/api/mono/track", body={
            "id": track["id"],
            "quality": quality,
            "lyrics": lyrics or {"mode": "off", "prefer": "synced"},
            "meta": {
                "title": track.get("title"),
                "artists": track.get("artists") or [],
                "artwork": track.get("artwork"),
                "duration": track.get("duration"),
            },
        })
        return str(data["job"])

    async def submit_album(self, release: dict, quality: str,
                           lyrics: Optional[dict] = None) -> str:
        data = await self._api("POST", "/api/mono/album", body={
            "id": release["id"],
            "quality": quality,
            "lyrics": lyrics or {"mode": "off", "prefer": "synced"},
            "meta": {
                "title": release.get("title"),
                "artists": release.get("artists") or [],
                "artwork": release.get("artwork"),
            },
        })
        return str(data["job"])

    async def job_status(self, job_id: str) -> dict:
        data = await self._api("GET", f"/api/jobs/{job_id}")
        return dict(data.get("job") or {})


    async def release(self, release_id: str) -> dict:
        data = await self._api("GET", f"/api/mono/release/{release_id}")
        return dict(data.get("data") or {})

    async def lyrics_search(self, query: str) -> list[dict]:
        data = await self._api("GET", "/api/lyrics/search",
                               params={"q": query})
        return list(data.get("data") or [])

    async def lyrics_get(self, artist: str, track: str,
                         album: Optional[str] = None,
                         duration: Optional[int] = None,
                         prefer: str = "synced") -> dict:
        params = {"artist": artist, "track": track, "prefer": prefer}
        if album:
            params["album"] = album
        # duration comes in as milliseconds (mono's track data). convert to
        # seconds and only send it when it's >= 1s: the hub rejects duration=0
        # with a 422, and flooring ms<1000 to 0 used to trigger exactly that.
        try:
            secs = int(duration) // 1000 if duration else 0
        except (TypeError, ValueError):
            secs = 0
        if secs >= 1:
            params["duration"] = secs
        data = await self._api("GET", "/api/lyrics/get", params=params)
        return dict(data.get("data") or {})


    async def search(self, query: str) -> dict:
        query = (query or "").strip()
        if not query:
            raise MonoError("empty search.")
        data: Optional[dict] = None
        try:
            resp = await self._http.get(
                f"{MONO_BASE}/search", params={"q": query}, timeout=20.0)
            if resp.status_code == 200:
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            data = None
        if data is None and self.server_configured:
            try:
                resp = await self._http.get(
                    f"{SERVER_URL}/api/mono/search", params={"q": query},
                    headers=self._headers())
                if resp.status_code == 200 and resp.json().get("ok"):
                    data = resp.json().get("data")
            except (httpx.HTTPError, ValueError):
                data = None
        if data is None:
            raise MonoError(
                "monochrome search is unreachable (tried direct and via the "
                "hub). it might be down."
            )
        return parse_search(data)

    async def find_track(self, query: str) -> Optional[dict]:
        text = (query or "").strip()
        if text.isdigit() and len(text) >= 6:
            return {"id": text, "title": None, "artists": [], "artwork": None,
                    "duration": 0, "release_id": None}
        result = await self.search(text)
        return result["tracks"][0] if result["tracks"] else None

    async def find_release(self, query: str) -> Optional[dict]:
        text = (query or "").strip()
        if text.isdigit() and len(text) >= 6:
            return {"id": text, "title": None, "artists": [], "artwork": None,
                    "year": "", "type": ""}
        result = await self.search(text)
        return result["releases"][0] if result["releases"] else None

    async def probe(self) -> tuple[bool, str]:
        started = time.monotonic()
        try:
            resp = await self._http.get(
                f"{MONO_BASE}/search", params={"q": "test"}, timeout=10.0)
            return resp.status_code == 200, f"{(time.monotonic() - started) * 1000:.0f} ms"
        except httpx.HTTPError as exc:
            return False, type(exc).__name__


    def config(self, user_id: int) -> dict:
        merged = dict(DEFAULT_CONFIG)
        stored = self._users.get(int(user_id))
        if isinstance(stored, dict):
            merged.update(stored)
        return merged

    def set_config(self, user_id: int, key: str, value) -> None:
        if key not in CONFIG_KEYS:
            raise MonoError(
                f"unknown config key {key!r} - known: {', '.join(CONFIG_KEYS)}."
            )
        text = str(value).strip().lower()
        if key == "quality" and text not in QUALITIES:
            raise MonoError(
                "quality must be one of: " + ", ".join(QUALITIES) + "."
            )
        if key == "lyrics_mode" and text not in LYRICS_MODES:
            raise MonoError(
                    "lyrics must be (off, embed, sidecard) embed: (written into the file's tags) or "
                    "sidecar: (a separate .lrc/.txt)."
            )
        if key == "lyrics_prefer" and text not in LYRICS_PREFERS:
            raise MonoError("lyrics-prefer must be synced or plain.")
        entry = self._users.setdefault(int(user_id), {})
        entry[key] = text
        self._persist()

    def reset_config(self, user_id: int) -> None:
        self._users.pop(int(user_id), None)
        self._persist()

    def quality(self, user_id: int) -> str:
        value = str(self.config(user_id).get("quality") or
                    DEFAULT_CONFIG["quality"]).lower()
        return value if value in QUALITIES else DEFAULT_CONFIG["quality"]

    def lyrics_cfg(self, user_id: int) -> dict:
        cfg = self.config(user_id)
        mode = str(cfg.get("lyrics_mode") or DEFAULT_CONFIG["lyrics_mode"]).lower()
        prefer = str(cfg.get("lyrics_prefer") or
                     DEFAULT_CONFIG["lyrics_prefer"]).lower()
        return {
            "mode": mode if mode in LYRICS_MODES else DEFAULT_CONFIG["lyrics_mode"],
            "prefer": prefer if prefer in LYRICS_PREFERS
            else DEFAULT_CONFIG["lyrics_prefer"],
        }


    def _load(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        if not self._store.exists():
            return
        try:
            data = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _logger.warning("monochrome config unreadable. starting empty")
            return
        raw = data.get("users", {}) if isinstance(data, dict) else {}
        self._users = {
            int(key): (value if isinstance(value, dict) else {})
            for key, value in raw.items()
            if str(key).isdigit()
        }

    def _persist(self) -> None:
        payload = {"users": {str(k): v for k, v in self._users.items()}}
        tmp = self._store.with_suffix(".json.temp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self._store)

