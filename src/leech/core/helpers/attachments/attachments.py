from __future__ import annotations

import discord


async def gather_attachments(message: discord.Message) -> list[discord.Attachment]:
    sources = list(message.attachments)
    ref = message.reference
    resolved = ref.cached_message if ref is not None else None
    if resolved is None and ref is not None and ref.message_id is not None:
        try:
            resolved = await message.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            resolved = None
    if resolved is not None:
        sources += resolved.attachments
    return sources
