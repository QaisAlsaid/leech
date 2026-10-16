from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import discord


@dataclass(frozen=True)
class Match:
    channel_id: Optional[int] = None
    author_id: Optional[int] = None
    invoker_id: Optional[int] = None
    command_name: Optional[str] = None
    interaction_only: bool = True
    after_id: Optional[int] = None
    include_ephemeral: bool = True

    def check(self, message: discord.Message) -> bool:
        if self.channel_id is not None and message.channel.id != self.channel_id:
            return False
        if self.author_id is not None and message.author.id != self.author_id:
            return False
        if self.after_id is not None and message.id <= self.after_id:
            return False
        meta = message.interaction
        if self.invoker_id is not None or self.command_name is not None:
            invoker = getattr(meta, "user", None)
            if self.invoker_id is not None and (
                invoker is None or invoker.id != self.invoker_id
            ):
                return False
            if self.command_name is not None and (
                meta is None or meta.name != self.command_name
            ):
                return False
        elif self.interaction_only and meta is None:
            return False
        if not self.include_ephemeral and bool(
            getattr(message.flags, "ephemeral", False)
        ):
            return False
        return True
