from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

import discord
import httpx

from leech.core.cog.cog import Cog
from leech.core.cog.context import CogContext
from leech.core.cog.error import CogError
from leech.core.command.mock.binding import MockBinding
from leech.core.command.native.binding import NativeBinding
from leech.core.command.runtime import Argument, Command
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.core.error import BaseError
import leech.core.helpers.text.splitter as splitter

_logger = logging.getLogger(__name__)

REPLY_LIMIT = 1990
LIST_CAP = 10         
ALBUM_TRACK_CAP = 60    
VIEW_CHUNKS = 3      
LYRICS_CONCURRENCY = 4  
ATTACH_BATCH = 10       

SERVER_URL = (
    os.getenv("LEECH_HUB_URL") or ""
).strip().rstrip("/")
SERVER_TOKEN = (
    os.getenv("LEECH_HUB_TOKEN")
    or ""
).strip()
MONO_BASE = (os.getenv("MONO_BASE") or "https://tracks.monochrome.st").strip().rstrip("/")
CONFIG_STORE = Path("persistent/v1/lrc/lrc.json")

_INDEX_RE = re.compile(r"^index=(\d+)$", re.IGNORECASE)
_PREFER_RE = re.compile(r"^prefer=(synced|plain|lrc|txt)$", re.IGNORECASE)
_ZIP_RE = re.compile(r"^zip=(on|off)$", re.IGNORECASE)


class LrcError(BaseError):
    def __init__(self, message: str):
        super().__init__(message=message, is_user_error=True)


def _parse_what(text: str) -> "tuple[str, int, Optional[str], Optional[bool]]":
    """'opeth windowpane index=2 prefer=plain zip=off'
    -> (query, 2, 'plain', False)."""
    words: list[str] = []
    index = 0
    prefer: Optional[str] = None
    zip_override: Optional[bool] = None
    for token in (text or "").split():
        m = _INDEX_RE.match(token)
        if m:
            index = int(m.group(1))
            continue
        m = _PREFER_RE.match(token)
        if m:
            raw = m.group(1).lower()
            prefer = {"lrc": "synced", "txt": "plain"}.get(raw, raw)
            continue
        m = _ZIP_RE.match(token)
        if m:
            zip_override = m.group(1).lower() == "on"
            continue
        if token.lower().startswith(("index=", "prefer=", "zip=")):
            raise CogError(
                f"didn't get `{token}` - it's index=N, "
                "prefer=synced|plain and zip=on|off.", is_user_error=True,
            )
        words.append(token)
    return " ".join(words).strip(), index, prefer, zip_override


def _pick_field(raw: dict, *keys):
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def _norm_hit(raw: dict) -> dict:
    plain = _pick_field(raw, "plainLyrics", "plain")
    try:
        duration_s = int(_pick_field(raw, "duration") or 0)
    except (TypeError, ValueError):
        duration_s = 0
    return {
        "id": raw.get("id"),
        "track": str(_pick_field(raw, "trackName", "track_name", "track", "title") or "?"),
        "artist": str(_pick_field(raw, "artistName", "artist_name", "artist") or "?"),
        "album": _pick_field(raw, "albumName", "album_name", "album"),
        "duration_s": duration_s,
        "instrumental": bool(raw.get("instrumental")),
        "plain_text": plain if isinstance(plain, str) and plain.strip() else None,
    }


def _fmt_secs(seconds) -> str:
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _pick_text(synced, plain, prefer: str) -> "tuple[Optional[str], str]":
    if prefer == "synced" and synced:
        return str(synced), "lrc"
    if prefer == "plain" and plain:
        return str(plain), "txt"
    if synced:
        return str(synced), "lrc"
    if plain:
        return str(plain), "txt"
    return None, ""


def _safe_name(text: str) -> str:
    safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in (text or ""))
    return safe.strip()[:120] or "lyrics"


class _Hub:
    def __init__(self):
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _api(self, method: str, path: str, params: dict | None = None,
                   body: dict | None = None) -> dict:
        try:
            if method == "GET":
                resp = await self._http.get(
                    SERVER_URL + path, params=params,
                    headers={"x-leech-token": SERVER_TOKEN})
            else:
                resp = await self._http.post(
                    SERVER_URL + path, json=body,
                    headers={"x-leech-token": SERVER_TOKEN})
        except httpx.HTTPError as exc:
            raise LrcError(
                f"leech-hub unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 401:
            raise LrcError(
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
            raise LrcError(f"leech-hub answered {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise LrcError(
                f"the leech-hub returned invalid json, details: {e}"
            ) from e
        if not data.get("ok"):
            raise LrcError(str(data.get("error") or "unknown hub error"))
        return data

    async def lyrics_search(self, query: str) -> list[dict]:
        data = await self._api("GET", "/api/lyrics/search", params={"q": query})
        return list(data.get("data") or [])

    async def lyrics_get(self, artist: str, track: str,
                         album: Optional[str] = None,
                         duration_s: Optional[int] = None,
                         prefer: Optional[str] = None) -> dict:
        params: dict = {"artist": artist, "track": track}
        if album:
            params["album"] = str(album)
        try:
            secs = int(duration_s or 0)
        except (TypeError, ValueError):
            secs = 0
        if secs >= 1:
            params["duration"] = secs
        if prefer:
            params["prefer"] = prefer
        data = await self._api("GET", "/api/lyrics/get", params=params)
        return dict(data.get("data") or {})

    async def mono_search(self, query: str) -> dict:
        """raw mono /search json - direct first, hub proxy as fallback."""
        try:
            resp = await self._http.get(
                f"{MONO_BASE}/search", params={"q": query}, timeout=20.0)
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, ValueError):
            pass
        data = await self._api("GET", "/api/mono/search", params={"q": query})
        return dict(data.get("data") or {})

    async def release(self, release_id: str) -> dict:
        data = await self._api("GET", f"/api/mono/release/{release_id}")
        return dict(data.get("data") or {})


class _LrcConfig:
    def __init__(self, store: Path = CONFIG_STORE):
        self._store = store
        self._users: dict[int, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._store.read_text(encoding="utf-8"))
            raw = data.get("users", {}) if isinstance(data, dict) else {}
            self._users = {
                int(key): (value if isinstance(value, dict) else {})
                for key, value in raw.items() if str(key).isdigit()
            }
        except (OSError, ValueError):
            self._users = {}

    def _persist(self) -> None:
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store.with_suffix(".json.temp")
            tmp.write_text(
                json.dumps({"users": {str(k): v for k, v in self._users.items()}}),
                encoding="utf-8")
            os.replace(tmp, self._store)
        except OSError as exc:
            _logger.warning("lrc config not saved: %s", exc)

    def album_zip(self, user_id: int) -> bool:
        value = self._users.get(int(user_id), {}).get("album_zip", "on")
        return str(value).strip().lower() not in ("off", "false", "0", "loose")

    def set_album_zip(self, user_id: int, value: str) -> bool:
        text = str(value or "").strip().lower()
        text = {"true": "on", "yes": "on", "1": "on", "zip": "on",
                "false": "off", "no": "off", "0": "off",
                "loose": "off"}.get(text, text)
        if text not in ("on", "off"):
            raise LrcError("zip must be on or off - `>lrc config zip off` "
                           "gets you the lyrics as loose files instead.")
        self._users.setdefault(int(user_id), {})["album_zip"] = text
        self._persist()
        return text == "on"

    def reset(self, user_id: int) -> None:
        self._users.pop(int(user_id), None)
        self._persist()


def _leaf(name: str, description: str, handler, arguments=None) -> Command:
    return Command(
        name=name,
        description=description,
        arguments=arguments or [],
        subcommands={},
        binding=NativeBinding(handler=handler),
    )


def _group(name: str, description: str, subcommands: dict) -> Command:
    return Command(
        name=name,
        description=description,
        arguments=[],
        subcommands=subcommands,
        binding=MockBinding(),
    )


_QUERY_ARG = [Argument(
    name="what",
    description="the query, plus optional index=N, "
                "prefer=synced|plain and zip=on|off.",
    usage_example="what=opeth windowpane index=1",
    type=TypesRegistry.require(Types.STR),
    required=True,
)]


class LrcCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        if not SERVER_URL or not SERVER_TOKEN:
            _logger.warning(
                "lrc cog: no leech hub (LEECH_HUB_URL / LEECH_HUB_TOKEN) "
                ". not loading"
            )
            return
        self.context.add_on_load(self.load)
        self.context.add_on_unload(self.shutdown)
        self.hub = _Hub()
        self._cfg = _LrcConfig()

    async def shutdown(self) -> None:
        if hasattr(self, "hub"):
            await self.hub.aclose()

    async def load(self):
        root = Command(
            name="lrc",
            description="lyrics off lrclib - search, view, download. tracks or whole albums.",
            usage_example=">lrc track lyrics view opeth windowpane",
            help_content=(
                "`>lrc track lyrics search <query>` lists hits with 0-based "
                "indices, then `view`/`download` take `index=N` (default 0) "
                "to pick one - no validation, first match wins unless you "
                "say otherwise. `prefer=synced|plain` overrides the flavor "
                "(view defaults to plain, download to synced .lrc). albums: "
                "the tracklist comes off mono and lyrics are fetched per "
                "track (first match); `index` picks the track. album "
                "download sends one zip, or loose files when "
                "`>lrc config zip off` (a one-off `zip=off` token works "
                "too). one track's lyrics failing never kills the run - "
                "the finals say which tracks came back empty."
            ),
            arguments=[],
            subcommands=(
                self.get_track()
                | self.get_album()
                | self.get_config()
            ),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(root.name, root):
            raise CogError(f"i failed to load '{root.name}' command.")

    # ------------------------------------------------------------- commands

    def get_track(self) -> dict[str, Command]:
        return {
            "track": _group("track", "track lyrics.", {
                "lyrics": _group("lyrics", "lyrics things.", {
                    "search": _leaf(
                        "search", "find lyrics by song name.",
                        self.track_search_handler, _QUERY_ARG),
                    "view": _leaf(
                        "view", "print a track's lyrics in chat.",
                        self.track_view_handler, _QUERY_ARG),
                    "download": _leaf(
                        "download", "get the .lrc/.txt as a file.",
                        self.track_download_handler, _QUERY_ARG),
                }),
            })
        }

    def get_album(self) -> dict[str, Command]:
        return {
            "album": _group("album", "album lyrics.", {
                "lyrics": _group("lyrics", "lyrics things.", {
                    "search": _leaf(
                        "search", "an album's tracks + lyrics status.",
                        self.album_search_handler, _QUERY_ARG),
                    "view": _leaf(
                        "view", "one track's lyrics (index picks the track).",
                        self.album_view_handler, _QUERY_ARG),
                    "download": _leaf(
                        "download", "the album's lyrics: zip, or loose files.",
                        self.album_download_handler, _QUERY_ARG),
                }),
            })
        }

    def get_config(self) -> dict[str, Command]:
        return {
            "config": _group("config", "your lrc settings (per user).", {
                "show": _leaf(
                    "show", "show your settings.", self.config_show_handler),
                "zip": _leaf(
                    "zip", "album downloads: one zip, or loose files.",
                    self.config_zip_handler,
                    [Argument(
                        name="value",
                        description="'on' (one zip, default) or 'off' "
                                    "(the lyrics as individual files).",
                        usage_example="value=off",
                        type=TypesRegistry.require(Types.STR),
                        required=True,
                    )]),
                "reset": _leaf(
                    "reset", "back to defaults.", self.config_reset_handler),
            })
        }

    async def _reply(self, message: discord.Message, text: str,
                     file: "discord.File | None" = None) -> discord.Message:
        return await message.reply(
            text[:REPLY_LIMIT],
            file=file,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _reply_files(self, message: discord.Message, text: str,
                           files: list[discord.File]) -> discord.Message:
        """discord caps a message at 10 attachments - callers batch."""
        return await message.reply(
            text[:REPLY_LIMIT],
            files=files[:ATTACH_BATCH],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_lines(self, message: discord.Message, lines: list[str]) -> None:
        payload = "\n".join(lines)
        target = message
        for chunk in splitter.split(payload):
            target = await self._reply(target, chunk)

    async def _send_code_chunks(self, message: discord.Message, head: str,
                                text: str, note: str,
                                download_hint: str) -> None:
        lines = text.splitlines()
        chunks: list[str] = []
        current: list[str] = []
        length = 0
        for line in lines:
            if length + len(line) + 1 > REPLY_LIMIT - 120 or len(current) > 60:
                chunks.append("\n".join(current))
                current, length = [], 0
            current.append(line)
            length += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        first = True
        target = message
        for chunk in chunks[:VIEW_CHUNKS]:
            head_line = f"{head} {note}" if first else f"{head} (cont.)"
            first = False
            target = await self._reply(
                target, f"{head_line}\n```\n{chunk[:REPLY_LIMIT - 40]}\n```")
        if len(chunks) > VIEW_CHUNKS:
            await self._reply(
                target,
                f"…(+{len(chunks) - VIEW_CHUNKS} more chunks — {download_hint})",
            )

    async def _hits(self, query: str) -> list[dict]:
        raw = await self.hub.lyrics_search(query)
        hits = [_norm_hit(item) for item in raw if isinstance(item, dict)]
        return [hit for hit in hits if hit["track"] != "?"]

    async def _track_lyrics(self, query: str, index: int,
                            prefer: Optional[str]
                            ) -> "tuple[dict, dict, Optional[str]]":
        hits = await self._hits(query)
        if not hits:
            raise LrcError(f"no lyrics results for {query!r}.")
        if index < 0 or index >= len(hits):
            raise LrcError(
                f"index {index} is out of range - results are 0-{len(hits) - 1}."
            )
        hit = hits[index]
        if hit["instrumental"]:
            raise LrcError(f"{hit['track']} is marked instrumental (no lyrics).")
        lyrics = await self.hub.lyrics_get(
            hit["artist"], hit["track"], hit.get("album"),
            hit.get("duration_s"), prefer)
        return hit, lyrics, hit.get("plain_text")

    @staticmethod
    def _track_texts(lyrics: dict, fallback_plain: Optional[str],
                     prefer: str) -> "tuple[Optional[str], str]":
        synced = lyrics.get("synced")
        plain = lyrics.get("plain") or fallback_plain
        return _pick_text(synced, plain, prefer)

    @staticmethod
    def _hit_flag(hit: dict) -> str:
        if hit.get("instrumental"):
            return "instrumental"
        if hit.get("plain_text"):
            return "plain"
        return "lrc?"   

    async def track_search_handler(self, invocation: Invocation):
        query, _, _, _ = _parse_what(invocation.arguments["what"])
        if not query:
            raise CogError(
                "give me something to search - "
                "`>lrc track lyrics search <name>`.", is_user_error=True,
            )
        hits = await self._hits(query)
        if not hits:
            await self._reply(invocation.message, f"no lyrics results for {query!r}.")
            return
        lines = [f'lyrics for "{query}" ({len(hits)}):']
        for index, hit in enumerate(hits[:LIST_CAP]):
            text = f"{index}. {hit['track']} - {hit['artist']}"
            if hit.get("album"):
                text += f" · {hit['album']}"
            if hit.get("duration_s"):
                text += f" · {_fmt_secs(hit['duration_s'])}"
            text += f" · {self._hit_flag(hit)}"
            lines.append(text)
        if len(hits) > LIST_CAP:
            lines.append(f"...(+{len(hits) - LIST_CAP} more)")
        lines.append("pick one: `>lrc track lyrics view <query> index=N` "
                     "(index defaults to 0)")
        await self._send_lines(invocation.message, lines)

    async def track_view_handler(self, invocation: Invocation):
        query, index, prefer, _ = _parse_what(invocation.arguments["what"])
        if not query:
            raise CogError(
                "give me something to view - "
                "`>lrc track lyrics view <name> index=N`.", is_user_error=True,
            )
        hit, lyrics, fallback_plain = await self._track_lyrics(
            query, index, prefer)
        want = prefer or "plain"
        text, kind = self._track_texts(lyrics, fallback_plain, want)
        if not text:
            raise LrcError(
                f"{hit['track']} - no lyrics i could pull for that one 😟"
            )
        head = f"**{hit['track']}** - {hit['artist']}"
        if hit.get("album"):
            head += f" · {hit['album']}"
        if hit.get("duration_s"):
            head += f" · {_fmt_secs(hit['duration_s'])}"
        await self._send_code_chunks(
            invocation.message, head, text, f"· {kind} · hit {index}",
            f"use `>lrc track lyrics download {query} index={index}`",
        )

    async def track_download_handler(self, invocation: Invocation):
        query, index, prefer, _ = _parse_what(invocation.arguments["what"])
        if not query:
            raise CogError(
                "give me something to download - "
                "`>lrc track lyrics download <name> index=N`.",
                is_user_error=True,
            )
        hit, lyrics, fallback_plain = await self._track_lyrics(
            query, index, prefer)
        want = prefer or "synced"
        text, ext = self._track_texts(lyrics, fallback_plain, want)
        if not text:
            raise LrcError(
                f"{hit['track']} - no lyrics file to build for that one 😟"
            )
        file = discord.File(
            io.BytesIO(text.encode("utf-8")),
            filename=f"{_safe_name(hit['artist'] + ' - ' + hit['track'])}.{ext}",
        )
        head = f"**{hit['track']}** - {hit['artist']} · {ext}"
        if hit.get("album"):
            head += f" · {hit['album']}"
        await self._reply(invocation.message, head, file=file)

    async def _album_context(self, query: str) -> "tuple[dict, list[dict]]":
        raw = await self.hub.mono_search(query)
        releases = [r for r in (raw.get("releases") or [])
                    if isinstance(r, dict) and r.get("id")]
        if not releases:
            raise LrcError(f"no album matched {query!r}.")
        rel = releases[0]
        data = await self.hub.release(str(rel["id"]))
        tracks = [t for t in (data.get("tracks") or []) if isinstance(t, dict)]
        if not tracks:
            raise LrcError("that release has no tracklist i can read.")
        artists = data.get("artists")
        if isinstance(artists, list) and artists:
            artists = ", ".join(str(a) for a in artists[:4])
        elif isinstance(rel.get("artistNames"), list):
            artists = ", ".join(str(a) for a in rel["artistNames"][:4])
        else:
            artists = "?"
        album = {
            "title": data.get("title") or rel.get("title") or f"album {rel['id']}",
            "artists": artists or "?",
            "artwork": data.get("artwork") or rel.get("artwork"),
        }
        return album, tracks[:ALBUM_TRACK_CAP]

    async def _probe_album(self, album: dict,
                           tracks: list[dict]) -> list[dict]:
        """one first-match lookup per track, capped concurrency. never raises
        for a single track - that track just comes back 'none'."""
        sem = asyncio.Semaphore(LYRICS_CONCURRENCY)
        album_title = album.get("title")

        async def one(idx: int, track: dict) -> dict:
            title = str(track.get("title") or f"track {idx + 1}")
            artist = ", ".join(str(a) for a in (track.get("artists") or [])) \
                or album["artists"]
            try:
                duration_s = int(track.get("duration") or 0) // 1000
            except (TypeError, ValueError):
                duration_s = 0
            entry = {
                "position": track.get("position") or idx + 1,
                "title": title,
                "artist": artist,
                "duration_s": max(0, duration_s),
                "found": False,
                "instrumental": False,
                "synced": None,
                "plain": None,
            }
            try:
                async with sem:
                    data = await self.hub.lyrics_get(
                        artist, title, album_title, entry["duration_s"])
            except Exception: 
                return entry  
            if data.get("found"):
                entry["found"] = True
                entry["instrumental"] = bool(data.get("instrumental"))
                entry["synced"] = data.get("synced")
                entry["plain"] = data.get("plain")
            return entry

        return list(await asyncio.gather(
            *(one(i, t) for i, t in enumerate(tracks))))

    @staticmethod
    def _entry_flag(entry: dict) -> str:
        if entry["found"] and entry["instrumental"]:
            return "instrumental"
        if entry["found"] and entry["synced"]:
            return "synced"
        if entry["found"] and entry["plain"]:
            return "plain"
        if entry["found"]:
            return "found"
        return "none"

    async def album_search_handler(self, invocation: Invocation):
        query, _, _, _ = _parse_what(invocation.arguments["what"])
        if not query:
            raise CogError(
                "give me an album - `>lrc album lyrics search <name>`.",
                is_user_error=True,
            )
        status = await self._reply(
            invocation.message, f"pulling {query!r} + checking lyrics...")
        album, tracks = await self._album_context(query)
        probed = await self._probe_album(album, tracks)
        found = sum(1 for e in probed if e["found"] and not e["instrumental"])
        lines = [f"**{album['title']}** - {album['artists']} · "
                 f"{len(tracks)} tracks · lyrics for {found}"]
        if album.get("artwork"):
            lines.append(f"[artwork]({album['artwork']})")
        for index, entry in enumerate(probed):
            lines.append(f"{index}. {entry['title']} · {self._entry_flag(entry)}")
        if len(tracks) == ALBUM_TRACK_CAP:
            lines.append(f"(capped at {ALBUM_TRACK_CAP} tracks)")
        lines.append("view one: `>lrc album lyrics view <album> index=N` "
                     "(track index, defaults to 0)")
        try:
            await status.delete()
        except discord.HTTPException:
            pass
        await self._send_lines(invocation.message, lines)

    async def album_view_handler(self, invocation: Invocation):
        query, index, prefer, _ = _parse_what(invocation.arguments["what"])
        if not query:
            raise CogError(
                "give me an album - `>lrc album lyrics view <name> index=N`.",
                is_user_error=True,
            )
        status = await self._reply(
            invocation.message, f"pulling {query!r} + checking lyrics...")
        album, tracks = await self._album_context(query)
        probed = await self._probe_album(album, tracks)
        if index < 0 or index >= len(probed):
            try:
                await status.delete()
            except discord.HTTPException:
                pass
            raise LrcError(
                f"index {index} is out of range - tracks are 0-{len(probed) - 1}."
            )
        entry = probed[index]
        if not entry["found"] or entry["instrumental"]:
            try:
                await status.delete()
            except discord.HTTPException:
                pass
            raise LrcError(
                f"no lyrics for {entry['title']!r} (first match). try the "
                "track commands: `>lrc track lyrics view <name>`."
            )
        want = prefer or "plain"
        text, kind = _pick_text(entry["synced"], entry["plain"], want)
        if not text:
            try:
                await status.delete()
            except discord.HTTPException:
                pass
            raise LrcError(f"{entry['title']} - nothing to show for that one 😟")
        head = (f"**{entry['title']}** - {entry['artist']} · "
                f"{album['title']} · track {entry['position']}")
        try:
            await status.delete()
        except discord.HTTPException:
            pass
        await self._send_code_chunks(
            invocation.message, head, text, f"· {kind} · track {index}",
            f"use `>lrc album lyrics download {query}`",
        )

    async def album_download_handler(self, invocation: Invocation):
        query, _, prefer, zip_override = _parse_what(
            invocation.arguments["what"])
        if not prefer:
            prefer = "synced"
        if not query:
            raise CogError(
                "give me an album - `>lrc album lyrics download <name>`.",
                is_user_error=True,
            )
        zip_on = (self._cfg.album_zip(invocation.message.author.id)
                  if zip_override is None else zip_override)
        status = await self._reply(
            invocation.message, f"pulling {query!r} + checking lyrics...")
        album, tracks = await self._album_context(query)
        probed = await self._probe_album(album, tracks)

        payloads: list[tuple[str, str]] = []
        no_lyrics: list[str] = []
        for entry in probed:
            try:
                pos = int(entry["position"] or 0)
            except (TypeError, ValueError):
                pos = 0
            stem = f"{pos:02d}. {entry['title']}"
            if entry["found"] and not entry["instrumental"]:
                text, ext = _pick_text(entry["synced"], entry["plain"], prefer)
                if text:
                    payloads.append((f"{_safe_name(stem)}.{ext}", text))
                    continue
            no_lyrics.append(stem + (" (instrumental)"
                                     if entry["instrumental"] else ""))
        found = len(payloads)

        try:
            await status.delete()
        except discord.HTTPException:
            pass
        if found == 0:
            raise LrcError(
                f"**{album['title']}** - none of its {len(probed)} tracks "
                "had lyrics, so nothing to send."
            )

        head = (f"**{album['title']}** - {album['artists']} · lyrics for "
                f"{found}/{len(probed)} tracks · prefer {prefer}")
        if no_lyrics:
            shown = ", ".join(no_lyrics[:8])
            if len(no_lyrics) > 8:
                shown += f" …(+{len(no_lyrics) - 8} more)"
            head += f"\nno lyrics: {shown}"

        if zip_on:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w",
                                 compression=zipfile.ZIP_DEFLATED) as zf:
                info: list[str] = [
                    f"{album['title']} — {album['artists']}",
                    f"tracks: {len(probed)}, preference: {prefer} "
                    "(first match, no validation)",
                    "",
                ]
                for name, text in payloads:
                    zf.writestr(name, text)
                    info.append(f"{name.rsplit('.', 1)[0]}: "
                                f"{name.rsplit('.', 1)[1]}")
                for miss in no_lyrics:
                    info.append(f"{miss}: no lyrics")
                zf.writestr("info.txt", "\n".join(info) + "\n")
            buffer.seek(0)
            base = _safe_name(album['artists'] + " - " + album['title'])
            file = discord.File(buffer, filename=f"{base} - lyrics.zip")
            await self._reply(invocation.message, head, file=file)
            return

        files = [discord.File(io.BytesIO(text.encode("utf-8")), filename=name)
                 for name, text in payloads]
        target = invocation.message
        for start in range(0, len(files), ATTACH_BATCH):
            batch = files[start:start + ATTACH_BATCH]
            label = head if start == 0 else (
                f"{album['title']} (cont. {start + 1}-"
                f"{start + len(batch)}/{len(files)})")
            target = await self._reply_files(target, label, batch)

    async def config_show_handler(self, invocation: Invocation):
        zip_on = self._cfg.album_zip(invocation.message.author.id)
        await self._reply(
            invocation.message,
            "your lrc config:\n"
            f"album zip: {'on (one zip)' if zip_on else 'off (loose .lrc/.txt files, 10 per message)'}\n"
            "(a one-off `zip=on|off` token on `>lrc album lyrics download` "
            "overrides it for that run)",
        )

    async def config_zip_handler(self, invocation: Invocation):
        zip_on = self._cfg.set_album_zip(
            invocation.message.author.id,
            invocation.arguments["value"])
        await self._reply(
            invocation.message,
            ("album lyrics downloads come as one zip again." if zip_on else
             "album lyrics downloads now come loose - the .lrc/.txt files "
             f"as attachments, up to {ATTACH_BATCH} per message."),
        )

    async def config_reset_handler(self, invocation: Invocation):
        self._cfg.reset(invocation.message.author.id)
        await self._reply(
            invocation.message,
            "lrc config reset - album zip on.",
        )


def create_cog(context: CogContext):
    return LrcCog(context=context)
