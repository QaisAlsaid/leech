DISCORD_MESSAGE_LIMIT = 2000


def split(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(current) + len(line) <= limit:
            current += line
            continue

        if current:
            chunks.append(current.rstrip())
            current = ""

        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]

        current = line

    if current:
        chunks.append(current.rstrip())

    return chunks
