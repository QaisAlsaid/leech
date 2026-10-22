from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from .registry import TypesRegistry
from .definition import TypeDefinition, Types
from leech.core.parser.error import ParseError

if TYPE_CHECKING:
    from leech.core.command.runtime import Argument


@dataclass
class AttachmentRef:
    url: str
    filename: str
    content_type: str | None = None


class AttachmentType(TypeDefinition):
    def __init__(self):
        super().__init__(
            type=Types.ATTACHMENT,
            description="an attachment (image or file) by direct url.",
            usage_example="value=https://cdn.discordapp.com/.../image.png",
            help_content="a direct url to a file. usually a discord cdn attachment link.",
        )

    @override
    def parse(self, raw: str, arg: Argument) -> AttachmentRef:
        if not raw.startswith(("http://", "https://")):
            raise ParseError(
                f"invalid attachment url: '{raw}', for argument: '{arg.name}'."
            )
        filename = raw.split("/")[-1].split("?")[0] or "attachment"
        return AttachmentRef(url=raw, filename=filename)
