from __future__ import annotations

import asyncio
import io
import time

import discord

from leech.core.cog.cog import Cog
from leech.core.cog.context import CogContext
from leech.core.cog.error import CogError
from leech.core.command.mock.binding import MockBinding
from leech.core.command.native.binding import NativeBinding
from leech.core.command.runtime import Argument, Command
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry
from .service import (
    MONO_BASE,
    QUALITIES,
    MonoError,
    MonoService,
    fmt_duration,
    quality_label,
)
import leech.core.helpers.text.paginator as paginator

REPLY_LIMIT = 1990
LIST_CAP = 10
ARTIST_CAP = 8
FIND_CAP = 15
VIEW_CAP = 60
PROGRESS_MIN_SECS = 3.0
POLL_SECS = 2.5
TRACK_JOB_TIMEOUT = 420.0
ALBUM_JOB_TIMEOUT = 2400.0


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


class MonoCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.context.add_on_unload(self.shutdown)
        self.mono = MonoService()

    async def shutdown(self) -> None:
        await self.mono.aclose()

    async def load(self):
        root = Command(
            name="mono",
            description="search and pull music off monochrome. (tracks, albums, artists).",
            usage_example=">mono track search rebirth of an old nation",
            help_content=(
                "search runs straight from leech, and flac track links come "
                " from monochrome itself. anything that needs "
                "converting (mp3/wav/m4a/opus), or whole album zip, or "
                "lyrics embedded in the tags runs as a job on the leech-hub."
                "\nyou can set your format, lyrics and album zip with `>mono config`."
            ),
            arguments=[],
            subcommands=(
                self.get_track()
                | self.get_album()
                | self.get_artist()
                | self.get_config()
                | self.get_status()
            ),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(root.name, root):
            raise CogError(f"i failed to load '{root.name}' command.")

    def get_track(self) -> dict[str, Command]:
        return {
            "track": _group(
                "track",
                "track commands.",
                {
                    "search": _leaf(
                        "search",
                        "find tracks by name.",
                        self.track_search_handler,
                        [
                            Argument(
                                name="name",
                                description="the track name.",
                                usage_example="name=moonlapse vertigo",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "download": _leaf(
                        "download",
                        "download a track by id or name (first match).",
                        self.track_download_handler,
                        [
                            Argument(
                                name="query",
                                description="a monochrome track id, or a name.",
                                usage_example="query=166617220766674944",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                },
            )
        }

    def get_album(self) -> dict[str, Command]:
        return {
            "album": _group(
                "album",
                "album commands.",
                {
                    "search": _leaf(
                        "search",
                        "find albums by name.",
                        self.album_search_handler,
                        [
                            Argument(
                                name="name",
                                description="the album name.",
                                usage_example="name=still life",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "view": _leaf(
                        "view",
                        "an album's tracklist + artwork, by id or name.",
                        self.album_view_handler,
                        [
                            Argument(
                                name="query",
                                description="a monochrome release id, or a name.",
                                usage_example="query=ghost reveries",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "download": _leaf(
                        "download",
                        "download an album (zip) by id or name (first match).",
                        self.album_download_handler,
                        [
                            Argument(
                                name="query",
                                description="a monochrome release id, or a name.",
                                usage_example="query=blessed he with boils",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                },
            )
        }

    def get_artist(self) -> dict[str, Command]:
        return {
            "artist": _group(
                "artist",
                "artist commands.",
                {
                    "search": _leaf(
                        "search",
                        "an artist's popular tracks and albums, with ids.",
                        self.artist_search_handler,
                        [
                            Argument(
                                name="name",
                                description="the artist name.",
                                usage_example="name=opeth",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "find": _leaf(
                        "find",
                        "search for artists - lists several matches.",
                        self.artist_find_handler,
                        [
                            Argument(
                                name="query",
                                description="the artist name (or a fragment).",
                                usage_example="query=pink floyd",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                },
            )
        }

    def get_config(self) -> dict[str, Command]:
        return {
            "config": _group(
                "config",
                "configure your monochrome settings (per user).",
                {
                    "show": _leaf(
                        "show", "show your settings.", self.config_show_handler
                    ),
                    "quality": _leaf(
                        "quality",
                        "set your download quality.",
                        self.config_quality_handler,
                        [
                            Argument(
                                name="value",
                                description="flac (original, instant link), mp3 "
                                "kbps (320 256 192 128 96 64), wav, "
                                "m4a, opus, copy.",
                                usage_example="value=320",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "lyrics": _leaf(
                        "lyrics",
                        "how should lyrics be downloaded (if any)",
                        self.config_lyrics_handler,
                        [
                            Argument(
                                name="value",
                                description="'off', 'embed' (inside the file's tags) "
                                "or 'sidecar' (a separate .lrc/.txt).",
                                usage_example="value=sidecar",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "lyrics-prefer": _leaf(
                        "lyrics-prefer",
                        "synced (.lrc, timestamps) or plain lyrics.",
                        self.config_lyrics_prefer_handler,
                        [
                            Argument(
                                name="value",
                                description="'synced' or 'plain'.",
                                usage_example="value=synced",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "zip": _leaf(
                        "zip",
                        "album downloads: one zip, or loose files.",
                        self.config_zip_handler,
                        [
                            Argument(
                                name="value",
                                description="'on' (one zip, default) or 'off' "
                                "(every track as its own link).",
                                usage_example="value=off",
                                type=TypesRegistry.require(Types.STR),
                                required=True,
                            )
                        ],
                    ),
                    "reset": _leaf(
                        "reset", "back to defaults.", self.config_reset_handler
                    ),
                },
            )
        }

    def get_status(self) -> dict[str, Command]:
        return {
            "status": _leaf(
                "status",
                "search probe + leech hub health.",
                self.status_handler,
            )
        }

    async def _reply(
        self, message: discord.Message, text: str, file: "discord.File | None" = None
    ) -> discord.Message:
        return await message.reply(
            text[:REPLY_LIMIT],
            file=file,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_lines(self, message: discord.Message, lines: list[str]) -> None:
        payload = "\n".join(lines)
        # v10.9: one paginated message instead of a splitter chunk spam.
        await paginator.paginate(message, payload, client=self.context.client)

    def _progress_text(self, note: str, job: dict) -> str:
        progress = job.get("progress") or {}
        stage = str(progress.get("stage") or "working")
        total = progress.get("total")
        text = note
        if isinstance(total, int) and total > 0:
            text += f" - {stage} {progress.get('done', 0)}/{total}"
        else:
            text += f" - {stage}"
        if progress.get("note"):
            text += f": {progress['note']}"
        return text[:REPLY_LIMIT]

    async def _run_job(
        self, message: discord.Message, job_id: str, note: str, timeout: float
    ) -> tuple[dict, discord.Message]:
        status_msg = await self._reply(message, f"{note} - queued...")
        last_stage = ""
        last_edit = 0.0
        deadline = time.monotonic() + timeout
        while True:
            job = await self.mono.job_status(job_id)
            state = str(job.get("status") or "")
            if state == "done":
                return dict(job.get("result") or {}), status_msg
            if state == "error":
                raise MonoError(
                    str(job.get("error") or "the job failed on the runner.")
                )
            stage = self._progress_text(note, job)
            now = time.monotonic()
            if stage != last_stage and now - last_edit >= PROGRESS_MIN_SECS:
                try:
                    await status_msg.edit(content=stage)
                except discord.HTTPException:
                    pass
                last_stage, last_edit = stage, now
            if now >= deadline:
                raise MonoError(
                    "gave up waiting for the runner (timeout). (it may still "
                    "finish and the link would be lost), you should try again 😁"
                )
            await asyncio.sleep(POLL_SECS)

    async def _finish(self, status_msg: discord.Message, text: str) -> None:
        try:
            await status_msg.edit(content=text[:REPLY_LIMIT])
        except discord.HTTPException:
            await self._reply(status_msg, text)

    async def track_search_handler(self, invocation: Invocation):
        name = invocation.arguments["name"]
        result = await self.mono.search(name)
        tracks = result["tracks"]
        if not tracks:
            await self._reply(invocation.message, f"no tracks for {name!r}.")
            return
        lines = [f'tracks for "{name}" ({len(tracks)}):']
        for index, track in enumerate(tracks[:LIST_CAP], start=1):
            who = ", ".join(track["artists"]) or "?"
            lines.append(
                f"{index}. {track['title']} - {who} · "
                f"{fmt_duration(track['duration'])} · `{track['id']}`"
            )
        if len(tracks) > LIST_CAP:
            lines.append(f"...(+{len(tracks) - LIST_CAP} more)")
        lines.append("try `>mono track download <id or name>`")
        await self._send_lines(invocation.message, lines)

    async def track_download_handler(self, invocation: Invocation):
        query = invocation.arguments["query"]
        user = invocation.message.author.id
        quality = self.mono.quality(user)
        lyrics_cfg = self.mono.lyrics_cfg(user)
        track = await self.mono.find_track(query)
        if track is None:
            await self._reply(invocation.message, f"no track matched {query!r}.")
            return
        title = track.get("title") or f"track {track['id']}"

        if quality in ("flac", "copy") and lyrics_cfg["mode"] != "embed":
            file = None
            if lyrics_cfg["mode"] == "sidecar" and self.mono.server_configured:
                file = await self._sidecar_attachment(track, lyrics_cfg)
            await self._reply(
                invocation.message,
                self._track_final(track, None, quality, lyrics_cfg),
                file=file,
            )
            return

        self.mono.require_server()
        note = f"downloading {title} ({quality_label(quality)})"
        job_id = await self.mono.submit_track(track, quality, lyrics_cfg)
        result, status_msg = await self._run_job(
            invocation.message, job_id, note, TRACK_JOB_TIMEOUT
        )
        await self._finish(
            status_msg, self._track_final(track, result, quality, lyrics_cfg)
        )

    async def _sidecar_attachment(
        self, track: dict, lyrics_cfg: dict
    ) -> "discord.File | None":
        try:
            artists = track.get("artists") or []
            lyrics = await self.mono.lyrics_get(
                ", ".join(artists),
                track.get("title") or "",
                track.get("album"),
                track.get("duration"),
                lyrics_cfg["prefer"],
            )
        except MonoError:
            return None
        if not lyrics.get("found") or lyrics.get("instrumental"):
            return None
        text, ext = _pick_text(lyrics, lyrics_cfg["prefer"])
        if not text:
            return None
        artists = track.get("artists") or []
        base = ", ".join(artists) or "lyrics"
        title = track.get("title") or "track"
        safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in f"{base} - {title}")
        return discord.File(
            io.BytesIO(text.encode("utf-8")), filename=f"{safe[:120]}.{ext}"
        )

    def _track_final(
        self, track: dict, result: dict | None, quality: str, lyrics_cfg: dict
    ) -> str:
        title = track.get("title") or f"track {track['id']}"
        artists = ", ".join(track.get("artists") or []) or "?"
        head = f"**{title}** - {artists}"
        if track.get("duration"):
            head += f" · {fmt_duration(track['duration'])}"
        head += f" · {quality_label(quality)}"
        if result and result.get("size"):
            head += f" · {result['size'] / 1e6:.1f} mb"
        lines = [head]
        url = None
        if result and result.get("url"):
            url = str(result["url"])
        elif result and result.get("path"):
            url = f"{self.mono.server_url}{result['path']}"
        elif result is None and quality in ("flac", "copy"):
            url = f"{MONO_BASE}/track/{track['id']}"
        if url:
            lines.append(f"[download]({url})")
        if result and result.get("lyrics_url"):
            synced = " (synced)" if result.get("lyrics_synced") else ""
            lines.append(f"[lyrics]({result['lyrics_url']}){synced}")
        elif result and result.get("lyrics_embedded"):
            lines.append("lyrics: embedded in the tags")
        artwork = (result or {}).get("artwork") or track.get("artwork")
        if artwork:
            lines.append(f"[artwork]({artwork})")
        return "\n".join(lines)

    async def album_search_handler(self, invocation: Invocation):
        name = invocation.arguments["name"]
        result = await self.mono.search(name)
        releases = result["releases"]
        if not releases:
            await self._reply(invocation.message, f"no albums for {name!r}.")
            return
        lines = [f'albums for "{name}" ({len(releases)}):']
        for index, release in enumerate(releases[:LIST_CAP], start=1):
            who = ", ".join(release["artists"]) or "?"
            when = f" · {release['year']}" if release.get("year") else ""
            kind = f" · {release['type']}" if release.get("type") else ""
            lines.append(
                f"{index}. {release['title']} - {who}{when}{kind} · `{release['id']}`"
            )
        if len(releases) > LIST_CAP:
            lines.append(f"...(+{len(releases) - LIST_CAP} more)")
        lines.append(
            "try `>mono album download <id or name>` " "or peek with `>mono album view`"
        )
        await self._send_lines(invocation.message, lines)

    async def album_view_handler(self, invocation: Invocation):
        query = invocation.arguments["query"]
        release = await self.mono.find_release(query)
        if release is None:
            await self._reply(invocation.message, f"no album matched {query!r}.")
            return
        self.mono.require_server()
        data = await self.mono.release(release["id"])
        tracks = list(data.get("tracks") or [])
        title = data.get("title") or release.get("title") or f"album {release['id']}"
        artists = ", ".join(data.get("artists") or release.get("artists") or []) or "?"
        when = f" · {data.get('year')}" if data.get("year") else ""
        kind = f" · {data.get('type')}" if data.get("type") else ""
        lines = [f"**{title}** - {artists}{when}{kind} · {len(tracks)} tracks"]
        artwork = data.get("artwork") or release.get("artwork")
        if artwork:
            lines.append(f"[artwork]({artwork})")
        for index, track in enumerate(tracks[:VIEW_CAP], start=1):
            position = track.get("position") or index
            who = ", ".join(track.get("artists") or []) or artists
            same = "" if who == artists else f" - {who}"
            lines.append(
                f"{position:02d}. {track['title']}{same} · "
                f"{fmt_duration(track.get('duration'))} · `{track['id']}`"
            )
        if len(tracks) > VIEW_CAP:
            lines.append(f"...(+{len(tracks) - VIEW_CAP} more)")
        lines.append("try `>mono album download <id or name>`")
        await self._send_lines(invocation.message, lines)

    async def album_download_handler(self, invocation: Invocation):
        query = invocation.arguments["query"]
        user = invocation.message.author.id
        quality = self.mono.quality(user)
        lyrics_cfg = self.mono.lyrics_cfg(user)
        zip_on = self.mono.album_zip(user)
        release = await self.mono.find_release(query)
        if release is None:
            await self._reply(invocation.message, f"no album matched {query!r}.")
            return
        self.mono.require_server()
        title = release.get("title") or f"album {release['id']}"
        note = (
            f"zipping {title} ({quality_label(quality)})"
            if zip_on
            else f"pulling {title} loose ({quality_label(quality)})"
        )
        job_id = await self.mono.submit_album(
            release, quality, lyrics_cfg, zip_enabled=zip_on
        )
        result, status_msg = await self._run_job(
            invocation.message, job_id, note, ALBUM_JOB_TIMEOUT
        )
        head, extra = self._album_final(release, result, lyrics_cfg)
        await self._finish(status_msg, head)
        if extra:
            await self._send_lines(invocation.message, extra)

    def _album_final(
        self, release: dict, result: dict, lyrics_cfg: dict | None = None
    ) -> "tuple[str, list[str]]":
        title = result.get("title") or release.get("title") or "album"
        artists = (
            ", ".join(result.get("artists") or release.get("artists") or []) or "?"
        )
        head = f"**{title}** - {artists}"
        if result.get("year") or release.get("year"):
            head += f" · {result.get('year') or release.get('year')}"
        if result.get("track_count"):
            head += f" · {result['track_count']} tracks"
        if result.get("quality"):
            head += f" · {quality_label(str(result['quality']))}"
        if result.get("size"):
            head += f" · {result['size'] / 1e6:.1f} mb"
        lines = [head]

        files = [
            f
            for f in (result.get("files") or [])
            if isinstance(f, dict) and f.get("url")
        ]
        if files:
            head += f" · {len(files)} files"
            extra: list[str] = [f"files ({len(files)}):"]
            for entry in files:
                line = f"{str(entry.get('name') or 'file')} · [file]({entry['url']})"
                if isinstance(entry.get("size"), int) and entry["size"]:
                    line += f" · {entry['size'] / 1e6:.1f} mb"
                if entry.get("lyrics_url"):
                    line += f" · [lyrics]({entry['lyrics_url']})"
                extra.append(line)
        else:
            extra = []
            url = result.get("url")
            if not url and result.get("path"):
                url = f"{self.mono.server_url}{result['path']}"
            if url:
                lines.append(f"[download zip]({url})")
            lyrics_found = result.get("lyrics_found")
            if isinstance(lyrics_found, int) and lyrics_cfg:
                total = result.get("track_count") or lyrics_found
                if lyrics_cfg.get("mode") == "embed":
                    lines.append(f"lyrics: embedded in {lyrics_found}/{total} files")
                elif lyrics_cfg.get("mode") == "sidecar":
                    lines.append(
                        f"lyrics: {lyrics_found}/{total} tracks came with "
                        "a .lrc/.txt in the zip"
                    )

        artwork = result.get("artwork") or release.get("artwork")
        if artwork:
            lines.append(f"[artwork]({artwork})")

        failed = [f for f in (result.get("failed") or []) if isinstance(f, dict)]
        if failed:
            shown = ", ".join(
                f"{_fail_pos(f)} {_fail_title(f)} ({str(f.get('error') or 'failed')[:60]})"
                for f in failed[:8]
            )
            note = (
                f"{len(failed)} track{'s' if len(failed) != 1 else ''} failed "
                f"(retried, then skipped): {shown}"
            )
            if len(failed) > 8:
                note += f" ...(+{len(failed) - 8} more)"
            extra.append(note)
        if result.get("zip_fallback"):
            extra.append(f"note: {result['zip_fallback']}")

        if not files:
            titles = [
                t.get("title") for t in (result.get("tracks") or []) if t.get("title")
            ]
            if titles:
                shown = " · ".join(titles[:10])
                if len(titles) > 10:
                    shown += f" · ...(+{len(titles) - 10})"
                extra.append(f"tracks: {shown}")
        return "\n".join(lines), extra

    async def artist_search_handler(self, invocation: Invocation):
        name = invocation.arguments["name"]
        result = await self.mono.search(name)
        artists = result["artists"]
        tracks = result["tracks"]
        releases = result["releases"]
        if not artists and not tracks:
            await self._reply(invocation.message, f"no artist matched {name!r}.")
            return
        lines: list[str] = []
        if artists:
            artist = artists[0]
            handle = f" (@{artist['username']})" if artist.get("username") else ""
            lines.append(f"**{artist['name']}**{handle} · `{artist['id']}`")
            if artist.get("bio"):
                lines.append(f"\"{str(artist['bio']).strip()[:160]}\"")
            if artist.get("avatar"):
                lines.append(f"[avatar]({artist['avatar']})")
        lines.append("top tracks:")
        for index, track in enumerate(tracks[:ARTIST_CAP], start=1):
            lines.append(
                f"{index}. {track['title']} · {fmt_duration(track['duration'])} · "
                f"`{track['id']}`"
            )
        lines.append("albums:")
        for index, release in enumerate(releases[:ARTIST_CAP], start=1):
            when = f" · {release['year']}" if release.get("year") else ""
            lines.append(f"{index}. {release['title']}{when} · `{release['id']}`")
        if len(tracks) > ARTIST_CAP:
            lines.append(f"…(+{len(tracks) - ARTIST_CAP} tracks)")
        if len(releases) > ARTIST_CAP:
            lines.append(f"…(+{len(releases) - ARTIST_CAP} albums)")
        await self._send_lines(invocation.message, lines)

    async def artist_find_handler(self, invocation: Invocation):
        query = invocation.arguments["query"]
        result = await self.mono.search(query)
        rows: dict[str, dict] = {}

        def bump(name: str, kind: str) -> None:
            clean = str(name or "").strip()
            key = clean.lower()
            if not key or len(key) > 80:
                return
            row = rows.setdefault(key, {"name": clean, "tracks": 0, "releases": 0})
            row[kind] += 1

        for track in result["tracks"]:
            for name in track.get("artists") or []:
                bump(name, "tracks")
        for release in result["releases"]:
            for name in release.get("artists") or []:
                bump(name, "releases")

        ranked = sorted(
            rows.values(),
            key=lambda r: (r["tracks"] + r["releases"], r["tracks"]),
            reverse=True,
        )
        explicit = result["artists"]
        by_name = {r["name"].lower(): r for r in ranked}
        for artist in explicit:
            key = str(artist.get("name") or "").strip().lower()
            row = by_name.get(key)
            if row is not None:
                row["id"] = artist.get("id")
                row["username"] = artist.get("username")
            else:
                ranked.insert(
                    0,
                    {
                        "name": str(artist.get("name") or "?"),
                        "tracks": 0,
                        "releases": 0,
                        "id": artist.get("id"),
                        "username": artist.get("username"),
                    },
                )

        if not ranked:
            await self._reply(invocation.message, f"no artist matched {query!r}.")
            return

        lines = [f"artists matching **{query}**:"]
        for index, row in enumerate(ranked[:FIND_CAP], start=1):
            parts = [f"{index}. **{row['name']}**"]
            if row.get("username"):
                parts.append(f"(@{row['username']})")
            counts = []
            if row["tracks"]:
                counts.append(f"{row['tracks']} tracks")
            if row["releases"]:
                counts.append(f"{row['releases']} albums")
            if counts:
                parts.append(" · ".join(counts))
            if row.get("id"):
                parts.append(f"· `{row['id']}`")
            lines.append(" ".join(parts))
        if len(ranked) > FIND_CAP:
            lines.append(f"...(+{len(ranked) - FIND_CAP} more)")
        lines.append(
            "view one with `>mono artist search <name>` "
            "(or `>mono album view <name>`)."
        )
        await self._send_lines(invocation.message, lines)

    async def config_show_handler(self, invocation: Invocation):
        user_id = invocation.message.author.id
        settings = self.mono.config(user_id)
        server = (
            f"set ({self.mono.server_url})"
            if self.mono.server_configured
            else "not set. jobs and lyrics off, only flac links will work"
        )
        quality = settings.get("quality", "flac")
        zip_on = self.mono.album_zip(user_id)
        await self._reply(
            invocation.message,
            f"your monochrome config:\n"
            f"quality: {quality} ({quality_label(quality)})\n"
            f"lyrics: {settings.get('lyrics_mode')} "
            f"(prefer {settings.get('lyrics_prefer')})\n"
            f"album zip: {'on (one zip)' if zip_on else 'off (loose files, one link per track)'}\n"
            f"hub: {server}",
        )

    async def config_quality_handler(self, invocation: Invocation):
        user_id = invocation.message.author.id
        value = invocation.arguments["value"]
        self.mono.set_config(user_id, "quality", value)
        quality = self.mono.quality(user_id)
        cfg = self.mono.lyrics_cfg(user_id)
        extra = (
            " - instant links directly from monochrome."
            if quality in ("flac", "copy") and cfg["mode"] != "embed"
            else " - runs as a job on the leech-hub."
        )
        await self._reply(
            invocation.message,
            f"quality set to {quality} ({quality_label(quality)}){extra}",
        )

    async def config_lyrics_handler(self, invocation: Invocation):
        self.mono.set_config(
            invocation.message.author.id, "lyrics_mode", invocation.arguments["value"]
        )
        cfg = self.mono.lyrics_cfg(invocation.message.author.id)
        explain = {
            "off": "no lyrics with downloads.",
            "embed": "lyrics get written into the file's tags (jobs, even for flac).",
            "sidecar": "a separate .lrc/.txt alongside the music files.",
        }[cfg["mode"]]
        await self._reply(
            invocation.message,
            f"lyrics set to {cfg['mode']} (prefer {cfg['prefer']}) - {explain}",
        )

    async def config_lyrics_prefer_handler(self, invocation: Invocation):
        self.mono.set_config(
            invocation.message.author.id, "lyrics_prefer", invocation.arguments["value"]
        )
        cfg = self.mono.lyrics_cfg(invocation.message.author.id)
        await self._reply(
            invocation.message,
            f"lyrics preference: {cfg['prefer']}"
            + (
                " (.lrc with timestamps)"
                if cfg["prefer"] == "synced"
                else " (plain text)"
            ),
        )

    async def config_zip_handler(self, invocation: Invocation):
        user_id = invocation.message.author.id
        self.mono.set_config(user_id, "album_zip", invocation.arguments["value"])
        zip_on = self.mono.album_zip(user_id)
        await self._reply(
            invocation.message,
            (
                "album downloads come as one zip again."
                if zip_on
                else "album downloads now come loose - every track gets its own "
                "link (+ a lyrics link when sidecars are on)."
            ),
        )

    async def config_reset_handler(self, invocation: Invocation):
        self.mono.reset_config(invocation.message.author.id)
        await self._reply(
            invocation.message,
            "config reset - quality flac, lyrics sidecar (prefer synced), "
            "album zip on.",
        )

    async def status_handler(self, invocation: Invocation):
        lines = ["mono:"]
        alive, detail = await self.mono.probe()
        lines.append(
            f"search: {'direct ok (' + detail + ')' if alive else 'down (' + detail + ')'}"
        )
        if self.mono.server_configured:
            try:
                health = await self.mono.health()
                runner = health.get("runner") or {}
                lines.append(
                    f"hub: {self.mono.server_url} · "
                    f"queue {health.get('queue_depth', 0)} · "
                    f"runner {'armed (' + str(runner.get('repo') or '?') + ')' if runner.get('dispatch') else 'dispatch off (janitor only, every 15 min)'}"
                )
            except MonoError as exc:
                lines.append(f"hub: unreachable - {exc.message}")
        else:
            lines.append(
                "hub: not set (LEECH_HUB_URL). jobs, album view and lyrics "
                "disabled; flac links still work"
            )
        quality = self.mono.quality(invocation.message.author.id)
        cfg = self.mono.lyrics_cfg(invocation.message.author.id)
        zip_on = self.mono.album_zip(invocation.message.author.id)
        lines.append(
            f"your quality: {quality} ({quality_label(quality)}) · "
            f"lyrics: {cfg['mode']} (prefer {cfg['prefer']}) · "
            f"album zip: {'on' if zip_on else 'off'} · "
            f"options: {', '.join(QUALITIES)}"
        )
        await self._reply(invocation.message, "\n".join(lines))


def _pick_text(lyrics: dict, prefer: str) -> "tuple[str | None, str]":
    synced = lyrics.get("synced")
    plain = lyrics.get("plain")
    if prefer == "synced" and synced:
        return str(synced), "lrc"
    if plain:
        return str(plain), "txt"
    if synced:
        return str(synced), "lrc"
    return None, ""


def _fail_pos(entry: dict) -> str:
    try:
        return f"{int(entry.get('position') or 0):02d}."
    except (TypeError, ValueError):
        return "?."


def _fail_title(entry: dict) -> str:
    return str(entry.get("title") or "track")[:80]


def create_cog(context: CogContext):
    return MonoCog(context=context)
