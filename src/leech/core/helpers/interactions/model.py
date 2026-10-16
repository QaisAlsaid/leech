from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import discord
from discord import components as dcomponents
from discord import http
import logging
import json

_logger = logging.getLogger(__name__)

V2_FLAG = 1 << 15
V2_TEXT = 10  # text display
V2_MEDIA = {11, 12, 13}  # thumbnail, media gallery, file

ECHO_WHEN = {"auto", "always", "never"}
ECHO_KINDS = {"text", "image", "video", "audio", "file", "buttons"}
ECHO_ALIASES = {
    "all": "always",
    "images": "image",
    "videos": "video",
    "audios": "audio",
    "files": "file",
    "interaction": "buttons",
    "button": "buttons",
}


def media_kind(url: str, content_type: Optional[str]) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith(("image/", "video/", "audio/")):
        return ct.rsplit("/", 1)[0]
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    return {
        "png": "image",
        "jpg": "image",
        "jpeg": "image",
        "gif": "image",
        "webp": "image",
        "bmp": "image",
        "avif": "image",
        "mp4": "video",
        "webm": "video",
        "mov": "video",
        "mkv": "video",
        "mp3": "audio",
        "ogg": "audio",
        "wav": "audio",
        "m4a": "audio",
        "flac": "audio",
    }.get(ext, "file")


@dataclass(frozen=True)
class EchoSpec:
    when: str = "auto"
    kinds: Optional[frozenset] = None  # None = everything

    def wants(self, kind: str) -> bool:
        return self.kinds is None or kind in self.kinds

    @classmethod
    def parse(cls, raw: Optional[str]) -> "EchoSpec":
        if raw is None:
            return cls()
        when, kinds = "auto", set()
        for word in str(raw).lower().split(","):
            word = ECHO_ALIASES.get(word.strip(), word.strip())
            if not word:
                continue
            if word == "media":
                kinds.update(("image", "video", "audio"))
            elif word in ECHO_WHEN:
                when = word
            elif word in ECHO_KINDS:
                kinds.add(word)
            else:
                raise ValueError(
                    f"unknown echo selector: '{word}' "
                    f"(kinds: {', '.join(sorted(ECHO_KINDS))} + media. when: auto/always/never)"
                )
        return cls(when=when, kinds=frozenset(kinds) or None)


def extract_v2(components) -> tuple[str, list[str]]:
    texts: list[str] = []
    media: list[str] = []
    seen: set[str] = set()

    def collect_urls(node) -> None:
        if isinstance(node, dict):
            url = node.get("url")
            if (
                isinstance(url, str)
                and url.startswith(("http://", "https://"))
                and url not in seen
            ):
                seen.add(url)
                media.append(url)
            for value in node.values():
                collect_urls(value)
        elif isinstance(node, list):
            for value in node:
                collect_urls(value)

    def walk(node) -> None:
        if isinstance(node, dict):
            ctype = node.get("type")
            if ctype == V2_TEXT and isinstance(node.get("content"), str):
                texts.append(node["content"])
            elif ctype in V2_MEDIA:
                collect_urls(node)
                return
            for key in ("components", "items", "accessory"):
                child = node.get(key)
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(components)
    return "\n".join(t for t in texts if t.strip()), media


async def _raw_message(message: discord.Message, client) -> Optional[dict]:
    try:
        route = http.Route(
            "GET", "/channels/{channel_id}/messages", channel_id=message.channel.id
        )
        data = await client.http.request(
            route, params={"limit": "3", "around": str(message.id)}
        )
    except discord.HTTPException as e:
        _logger.warning("v2 raw fetch failed: %s", e)
        return None
    for entry in data or []:
        if str(entry.get("id")) == str(message.id):
            return entry
    return None


async def harvest_full(message: discord.Message, client) -> HarvestedMessage:
    harvested = harvest(message)
    if not bool(getattr(message.flags, "value", 0) & V2_FLAG):
        return harvested
    raw = await _raw_message(message, client)
    if raw is None:
        return harvested
    text, media = extract_v2(raw.get("components") or [])
    if text:
        harvested.content = (harvested.content + "\n" + text).strip()
    harvested.v2_media = media
    known = {a.url for a in harvested.attachments}
    for att in raw.get("attachments") or []:
        url = att.get("url")
        if url and url not in known:
            known.add(url)
            harvested.attachments.append(
                HarvestedAttachment(
                    filename=att.get("filename") or "attachment",
                    url=url,
                    content_type=att.get("content_type"),
                    size=att.get("size") or 0,
                )
            )
    _logger.warning("v2 raw: %.2500s", json.dumps(raw, default=str))
    _logger.warning(
        "v2 extract: text=%r media=%d attachments=%d",
        text[:120],
        len(media),
        len(raw.get("attachments") or []),
    )
    return harvested


def media_name(url: str, index: int) -> str:
    stem = url.split("/")[-1].split("?")[0].rsplit(".", 1)[0]
    stem = stem.replace("-", "").replace("_", "")
    return stem if stem.isalnum() else f"image {index}"


@dataclass
class HarvestedField:
    name: str
    value: str
    inline: bool = False


def _url_filename(url: str) -> str:
    name = url.split("/")[-1].split("?")[0]
    return name or "image"


@dataclass
class HarvestedEmbed:
    title: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    author: Optional[str] = None
    footer: Optional[str] = None
    image: Optional[str] = None
    images: list[str] = field(default_factory=list)
    thumbnail: Optional[str] = None
    fields: list[HarvestedField] = field(default_factory=list)

    def render(self) -> list[str]:
        lines: list[str] = []
        if self.author:
            lines.append(f"- {self.author}")
        if self.title:
            lines.append(self.title)
        if self.description:
            lines.append(self.description)
        lines += [f"{f.name}: {f.value}" for f in self.fields]
        if self.footer:
            lines.append(f"- {self.footer}")
        return lines


@dataclass
class HarvestedAttachment:
    filename: str
    url: str
    content_type: Optional[str] = None
    size: int = 0


@dataclass
class HarvestedComponent:
    kind: str
    custom_id: Optional[str] = None
    label: Optional[str] = None
    style: Optional[str] = None
    emoji: Optional[str] = None
    disabled: bool = False
    placeholder: Optional[str] = None
    options: list[str] = field(default_factory=list)

    def render(self) -> str:
        if self.kind == "button":
            name = self.label or self.emoji or "?"
            return (
                f"{'(' if self.disabled else '['}{name}{')' if self.disabled else ']'}"
            )
        opts = f" ({len(self.options)} options)" if self.options else ""
        return f"[select: {self.placeholder or 'menu'}{opts}]"


@dataclass
class HarvestedMessage:
    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    embeds: list[HarvestedEmbed] = field(default_factory=list)
    attachments: list[HarvestedAttachment] = field(default_factory=list)
    components: list[HarvestedComponent] = field(default_factory=list)
    created_at: Optional[datetime] = None
    is_ephemeral: bool = False
    v2_media: list[str] = field(default_factory=list)
    guild_id: Optional[int] = None

    def render(self, limit: int = 1900) -> str:
        parts: list[str] = []
        if self.content:
            parts.append(self.content)
        for embed in self.embeds:
            parts.extend(embed.render())
        parts += [f"[attachment: {a.filename}] {a.url}" for a in self.attachments]
        if self.components:
            parts.append(
                f"(buttons on message {self.message_id}: "
                + " ".join(c.render() for c in self.components)
                + ")"
            )
        return "\n".join(p for p in parts if p)[:limit]

    def render_compact(
        self,
        limit: int = 1500,
        include_titles: bool = True,
        include_components: bool = True,
    ) -> str:
        lines: list[str] = []
        if self.content:
            lines.append(self.content)
        for index, embed in enumerate(self.embeds):
            if index == 0:
                if embed.author:
                    lines.append(embed.author)
                if embed.description:
                    lines.append(embed.description)
            if include_titles and embed.title:
                lines.append(embed.title)
        if self.components and include_components:
            lines.append(" ".join(c.render() for c in self.components))
        if self.embeds and self.embeds[0].footer:
            lines.append(self.embeds[0].footer)
        lines += [f"[file: {a.filename}]" for a in self.attachments]
        return "\n".join(line for line in lines if line)[:limit]

    def image_urls(self) -> list[str]:
        out, seen = [], set()
        for embed in self.embeds:
            for url in (embed.image, embed.thumbnail, *embed.images):
                if url and url not in seen:
                    seen.add(url)
                    out.append(url)
        for url in self.v2_media:
            if url not in seen and media_kind(url, None) == "image":
                seen.add(url)
                out.append(url)
        return out

    def attachments_of(self, kinds) -> list[HarvestedAttachment]:
        return [
            a for a in self.attachments if media_kind(a.url, a.content_type) in kinds
        ]

    def media_pairs(self) -> list[str]:
        out: list[str] = []
        for embed in self.embeds:
            title = (embed.title or "image").replace("[", "(").replace("]", ")")
            for i, url in enumerate((embed.image, embed.thumbnail, *embed.images)):
                if url:
                    out.append(f"[{title if title else f'media#{i}'}]({url})")
        return out

    def media_urls(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for att in self.attachments:
            if att.url not in seen:
                seen.add(att.url)
                out.append((att.url, att.filename or "attachment"))
        for embed in self.embeds:
            for url in (embed.image, embed.thumbnail, *embed.images):
                if url and url not in seen:
                    seen.add(url)
                    out.append((url, _url_filename(url)))
        return out

    def file_media(self) -> list[tuple[str, str]]:
        return [(a.url, a.filename or "attachment") for a in self.attachments]

    def image_media(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for embed in self.embeds:
            for url in (embed.image, embed.thumbnail, *embed.images):
                if url and url not in seen:
                    seen.add(url)
                    out.append(url)
        for embed in self.v2_media:
            out.append(embed)
        return out


def harvest(message: discord.Message) -> HarvestedMessage:
    def _components() -> list[HarvestedComponent]:
        out: list[HarvestedComponent] = []
        for row in message.components or []:
            for comp in getattr(row, "children", None) or [row]:
                if isinstance(comp, dcomponents.Button):
                    out.append(
                        HarvestedComponent(
                            kind="button",
                            custom_id=comp.custom_id,
                            label=comp.label,
                            style=getattr(comp.style, "name", None),
                            emoji=(str(comp.emoji) if comp.emoji else None),
                            disabled=comp.disabled,
                        )
                    )
                elif isinstance(comp, dcomponents.SelectMenu):
                    out.append(
                        HarvestedComponent(
                            kind="select",
                            custom_id=comp.custom_id,
                            placeholder=comp.placeholder,
                            disabled=comp.disabled,
                            options=[o.label for o in comp.options],
                        )
                    )
        return out

    guild = getattr(message.channel, "guild", None)

    return HarvestedMessage(
        message_id=message.id,
        channel_id=message.channel.id,
        author_id=message.author.id,
        author_name=str(message.author),
        content=message.content or "",
        guild_id=guild.id if guild else None,
        embeds=[
            HarvestedEmbed(
                title=e.title or None,
                description=e.description or None,
                url=e.url or None,
                author=getattr(e.author, "name", None),
                footer=getattr(e.footer, "text", None),
                image=(getattr(e.image, "url", None) or None),
                thumbnail=(getattr(e.thumbnail, "url", None) or None),
                fields=[
                    HarvestedField(f.name or "", f.value or "", f.inline)
                    for f in e.fields
                ],
                images=[
                    (getattr(img, "url", None) or str(img))
                    for img in (getattr(e, "images", None) or [])
                ],
            )
            for e in message.embeds
        ],
        attachments=[
            HarvestedAttachment(a.filename, a.url, a.content_type, a.size)
            for a in message.attachments
        ],
        is_ephemeral=bool(getattr(message.flags, "ephemeral", False)),
        components=_components(),
        created_at=message.created_at,
    )
