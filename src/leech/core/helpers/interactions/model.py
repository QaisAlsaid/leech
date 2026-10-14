from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import discord
from discord import components as dcomponents


@dataclass
class HarvestedField:
    name: str
    value: str
    inline: bool = False


@dataclass
class HarvestedEmbed:
    title: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    author: Optional[str] = None
    footer: Optional[str] = None
    image: Optional[str] = None
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

    def render(self, limit: int = 1900) -> str:
        parts: list[str] = []
        if self.content:
            parts.append(self.content)
        for embed in self.embeds:
            parts.extend(embed.render())
        parts += [f"[attachment: {a.filename}] {a.url}" for a in self.attachments]
        if self.components:
            parts.append(" ".join(c.render() for c in self.components))
        return "\n".join(p for p in parts if p)[:limit]


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

    return HarvestedMessage(
        message_id=message.id,
        channel_id=message.channel.id,
        author_id=message.author.id,
        author_name=str(message.author),
        content=message.content or "",
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
            )
            for e in message.embeds
        ],
        attachments=[
            HarvestedAttachment(a.filename, a.url, a.content_type, a.size)
            for a in message.attachments
        ],
        components=_components(),
        created_at=message.created_at,
    )
