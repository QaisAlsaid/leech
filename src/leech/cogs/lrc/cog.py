from __future__ import annotations

import io
import zipfile

import discord

from leech.core.cog.cog import Cog
from leech.core.cog.context import CogContext
from leech.core.cog.error import CogError
from leech.core.command.mock.binding import MockBinding
from leech.core.command.native.binding import NativeBinding
from leech.core.command.runtime import Argument, Command
from leech.core.command.invocation import Invocation
from leech.core.parser.types.registry import Types, TypesRegistry
from leech.cogs.mono.service import (
    MonoError,
    MonoService,
    fmt_duration,
)
import leech.core.helpers.text.splitter as splitter

REPLY_LIMIT = 1990
SEARCH_CAP = 8
ALBUM_TRACK_CAP = 60 
PREVIEW_LINES = 10
VIEW_CHUNKS = 3     
LYRICS_NOTES = {
    "synced": "· synced (.lrc)",
    "plain": "· plain",
    "instrumental": "· instrumental (no lyrics by design)",
}


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


def _query_argument() -> Argument:
    return Argument(
        name="query",
        description="a song ('artist - title') or album name.",
        usage_example="query=opeth windowpane",
        type=TypesRegistry.require(Types.STR),
        required=True,
    )


class MusicCog(Cog):
    def __init__(self, context: CogContext):
        super().__init__(context=context)
        self.context.add_on_load(self.load)
        self.context.add_on_unload(self.shutdown)
        self.mono = MonoService()

    async def shutdown(self) -> None:
        await self.mono.aclose()

    async def load(self):
        root = Command(
            name="lrc",
            description="lyrics lookups. (tracks and whole albums).",
            usage_example=">lrc track lyrics view xanthochroid deus absconditus pt.2",
            help_content=(
                "lyrics via **lrclib** (through the leech-hub, so they're validated "
                "against the real artist/album and cached). `view` prints "
                "them here, `download` attaches the .lrc/.txt, and the "
                "album variants do the whole tracklist at once. "
                "synced/plain preference is `>monochrome config "
                "lyrics-prefer` apply."
            ),
            arguments=[],
            subcommands=(
                self.get_track()
                | self.get_album()
            ),
            binding=MockBinding(),
            is_root=True,
        )
        if not self.context.add_command(root.name, root):
            raise CogError(f"i failed to load '{root.name}' command.")


    def get_track(self) -> dict[str, Command]:
        return {
            "track": _group("track", "track lyrics.", {
                "lyrics": _group("lyrics", "lyrics commands.", {
                    "search": _leaf(
                        "search", "find lyrics by song name.",
                        self.track_search_handler, [_query_argument()],
                    ),
                    "view": _leaf(
                        "view", "print a track's lyrics in chat.",
                        self.track_view_handler, [_query_argument()],
                    ),
                    "download": _leaf(
                        "download", "get the .lrc/.txt as a file.",
                        self.track_download_handler, [_query_argument()],
                    ),
                }),
            })
        }

    def get_album(self) -> dict[str, Command]:
        return {
            "album": _group("album", "album lyrics.", {
                "lyrics": _group("lyrics", "lyrics things.", {
                    "search": _leaf(
                        "search", "find albums by name.",
                        self.album_search_handler, [_query_argument()],
                    ),
                    "view": _leaf(
                        "view", "per-track lyrics status + a preview.",
                        self.album_view_handler, [_query_argument()],
                    ),
                    "download": _leaf(
                        "download", "the album's lyrics as a zip.",
                        self.album_download_handler, [_query_argument()],
                    ),
                }),
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

    async def _send_lines(self, message: discord.Message, lines: list[str]) -> None:
        payload = "\n".join(lines)
        target = message
        for chunk in splitter.split(payload):
            target = await self._reply(target, chunk)

    def _require_hub(self) -> None:
        self.mono.require_server()

    def _prefer(self, user_id: int) -> str:
        return self.mono.lyrics_cfg(user_id)["prefer"]

    def _pick(self, lyrics: dict, prefer: str) -> "tuple[str | None, str]":
        synced = lyrics.get("synced")
        plain = lyrics.get("plain")
        if prefer == "synced" and synced:
            return str(synced), "lrc"
        if plain:
            return str(plain), "txt"
        if synced:
            return str(synced), "lrc"
        return None, ""

    async def _best_track_lyrics(self, query: str, prefer: str) -> "tuple[dict, dict | None]":
        hits = await self.mono.lyrics_search(query)
        if not hits:
            raise MonoError(f"no lyrics matches for {query!r}.")
        best = hits[0]
        lyrics = await self.mono.lyrics_get(
            str(best.get("artist") or ""), str(best.get("track") or ""),
            best.get("album"), best.get("duration"), prefer)
        return best, lyrics

    async def track_search_handler(self, invocation: Invocation):
        query = invocation.arguments["query"]
        hits = await self.mono.lyrics_search(query)
        if not hits:
            await self._reply(invocation.message, f"no lyrics matches for {query!r}.")
            return
        lines = [f'lyrics matches for "{query}" ({len(hits)}):']
        for index, hit in enumerate(hits[:SEARCH_CAP], start=1):
            kinds = ("synced" if hit.get("synced") else "") + \
                    ("+plain" if hit.get("plain") else "")
            note = f" · {kinds}" if kinds else " · none on file"
            lines.append(
                f"{index}. {hit.get('track')} - {hit.get('artist')} · "
                f"{fmt_duration((hit.get('duration') or 0) * 1000)}{note}"
            )
        if len(hits) > SEARCH_CAP:
            lines.append(f"...(+{len(hits) - SEARCH_CAP} more)")
        lines.append("view one: `>lrc track lyrics view <name>`")
        await self._send_lines(invocation.message, lines)

    async def track_view_handler(self, invocation: Invocation):
        self._require_hub()
        query = invocation.arguments["query"]
        prefer = self._prefer(invocation.message.author.id)
        best, lyrics = await self._best_track_lyrics(query, prefer)
        head = (f"**{best.get('track')}** - {best.get('artist')}"
                + (f" · {best.get('album')}" if best.get("album") else ""))
        if not lyrics.get("found"):
            await self._reply(
                invocation.message,
                f"{head}\nno validated lyrics for that - the closest lrclib "
                "match failed the artist/track/album check.",
            )
            return
        if lyrics.get("instrumental"):
            await self._reply(invocation.message, f"{head} · instrumental")
            return
        note = LYRICS_NOTES.get(str(lyrics.get("chosen"))) or ""
        text, _ = self._pick(lyrics, prefer)
        if not text:
            await self._reply(invocation.message, f"{head} · nothing to show")
            return
        await self._send_code_chunks(invocation.message, head, text, note)

    async def _send_code_chunks(self, message: discord.Message, head: str,
                                text: str, note: str) -> None:
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
                f"...(+{len(chunks) - VIEW_CHUNKS} more chunks. use "
                "`>lrc track lyrics download` for the whole file)",
            )

    async def track_download_handler(self, invocation: Invocation):
        self._require_hub()
        query = invocation.arguments["query"]
        prefer = self._prefer(invocation.message.author.id)
        best, lyrics = await self._best_track_lyrics(query, prefer)
        if not lyrics.get("found") or lyrics.get("instrumental"):
            await self._reply(
                invocation.message,
                f"no lyrics file for that one - try `>lrc track lyrics "
                f"search {query}` and check the exact name.",
            )
            return
        text, ext = self._pick(lyrics, prefer)
        if not text:
            await self._reply(invocation.message, "nothing to attach for that.")
            return
        safe = "".join(c if c not in '\\/:*?"<>|' else "_"
                       for c in f"{best.get('artist')} - {best.get('track')}")
        file = discord.File(io.BytesIO(text.encode("utf-8")),
                            filename=f"{safe[:120]}.{ext}")
        await self._reply(
            invocation.message,
            f"**{best.get('track')}** - {best.get('artist')} · {ext}",
            file=file,
        )


    async def album_search_handler(self, invocation: Invocation):
        query = invocation.arguments["query"]
        result = await self.mono.search(query)
        releases = result["releases"]
        if not releases:
            await self._reply(invocation.message, f"no albums for {query!r}.")
            return
        lines = [f'albums for "{query}" ({len(releases)}):']
        for index, release in enumerate(releases[:SEARCH_CAP], start=1):
            who = ", ".join(release["artists"]) or "?"
            when = f" · {release['year']}" if release.get("year") else ""
            lines.append(f"{index}. {release['title']} - {who}{when} · `{release['id']}`")
        lines.append("lyrics for one: `>lrc album lyrics view <id or name>`")
        await self._send_lines(invocation.message, lines)

    async def _album_tracks(self, query: str) -> "tuple[dict, list[dict]]":
        release = await self.mono.find_release(query)
        if release is None:
            raise MonoError(f"no album matched {query!r}.")
        self._require_hub()
        data = await self.mono.release(release["id"])
        tracks = list(data.get("tracks") or [])
        if not tracks:
            raise MonoError("that release has no tracklist i can read.")
        album = {
            "title": data.get("title") or release.get("title") or "album",
            "artists": data.get("artists") or release.get("artists") or [],
            "year": data.get("year") or release.get("year"),
            "artwork": data.get("artwork") or release.get("artwork"),
        }
        return album, tracks[:ALBUM_TRACK_CAP]

    async def _album_lyrics(self, album: dict, tracks: list[dict],
                            prefer: str) -> list["tuple[dict, dict]"]:
        artist = ", ".join(album.get("artists") or [])
        out: list[tuple[dict, dict]] = []
        for track in tracks:
            t_artist = ", ".join(track.get("artists") or []) or artist
            try:
                lyrics = await self.mono.lyrics_get(
                    t_artist, track.get("title") or "",
                    album.get("title"), track.get("duration"), prefer)
            except MonoError:
                lyrics = {"found": False, "reason": "hub unreachable"}
            out.append((track, lyrics))
        return out

    async def album_view_handler(self, invocation: Invocation):
        self._require_hub()
        query = invocation.arguments["query"]
        prefer = self._prefer(invocation.message.author.id)
        album, tracks = await self._album_tracks(query)
        pairs = await self._album_lyrics(album, tracks, prefer)
        artists = ", ".join(album.get("artists") or []) or "?"
        when = f" · {album.get('year')}" if album.get("year") else ""
        found = sum(1 for _, ly in pairs if ly.get("found")
                    and not ly.get("instrumental"))
        lines = [f"**{album['title']}** - {artists}{when} · "
                 f"lyrics for {found}/{len(pairs)} tracks"]
        if album.get("artwork"):
            lines.append(f"[artwork]({album['artwork']})")
        preview_done = False
        for index, (track, lyrics) in enumerate(pairs, start=1):
            position = track.get("position") or index
            if lyrics.get("found") and lyrics.get("instrumental"):
                mark = "· instrumental"
            elif lyrics.get("found"):
                mark = "· synced" if lyrics.get("chosen") == "synced" else "· plain"
            else:
                mark = "- none"
            lines.append(f"{position:02d}. {track.get('title')} {mark}")
            if not preview_done and lyrics.get("found") \
                    and not lyrics.get("instrumental"):
                text, _ = self._pick(lyrics, prefer)
                if text:
                    snippet = "\n".join(text.splitlines()[:PREVIEW_LINES])
                    lines.append(f"```{snippet}```")
                    preview_done = True
        lines.append("get them all: `>lrc album lyrics download <id or name>`")
        await self._send_lines(invocation.message, lines)

    async def album_download_handler(self, invocation: Invocation):
        self._require_hub()
        query = invocation.arguments["query"]
        prefer = self._prefer(invocation.message.author.id)
        album, tracks = await self._album_tracks(query)
        pairs = await self._album_lyrics(album, tracks, prefer)
        artists = ", ".join(album.get("artists") or []) or "?"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            info: list[str] = [
                f"{album['title']} - {artists}",
                f"tracks: {len(pairs)}, preference: {prefer}",
                "",
            ]
            found = 0
            for index, (track, lyrics) in enumerate(pairs, start=1):
                position = track.get("position") or index
                stem = f"{position:02d}. {track.get('title') or 'track'}"
                if lyrics.get("found") and not lyrics.get("instrumental"):
                    text, ext = self._pick(lyrics, prefer)
                    if text:
                        zf.writestr(f"{stem}.{ext}", text)
                        found += 1
                        info.append(f"{stem}: {ext} "
                                    f"({lyrics.get('chosen') or 'ok'})")
                        continue
                info.append(f"{stem}: no lyrics"
                            + (" (instrumental)" if lyrics.get("instrumental") else ""))
            zf.writestr("info.txt", "\n".join(info) + "\n")
        if found == 0:
            await self._reply(
                invocation.message,
                f"**{album['title']}** - none of its {len(pairs)} tracks had "
                "validated lyrics, so no zip.",
            )
            return
        buffer.seek(0)
        safe = "".join(c if c not in '\\/:*?"<>|' else "_"
                       for c in f"{artists} - {album['title']}")[:120]
        file = discord.File(buffer, filename=f"{safe} - lyrics.zip")
        await self._reply(
            invocation.message,
            f"**{album['title']}** - {artists} · lyrics for {found}/{len(pairs)} "
            f"tracks · prefer {prefer}",
            file=file,
        )


def create_cog(context: CogContext):
    return MusicCog(context=context)
