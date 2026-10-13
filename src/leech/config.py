import os

_owner_id_str = os.getenv("OWNER_ID")
_bot_id_str = os.getenv("BOT_ID")
ai_api_key = os.getenv("AI_API_KEY")

if not _bot_id_str:
    raise ValueError("failed to load 'BOT_ID' environment variable.")
if not _owner_id_str:
    raise ValueError("failed to load 'OWNER_ID' environment variable.")
if not ai_api_key:
    raise ValueError("failed to load 'AI_API_KEY' environment variable.")

try:
    bot_id: int = int(_bot_id_str)

    owner_ids: list[int] = [
        int(owner_id.strip())
        for owner_id in _owner_id_str.split(",")
        if owner_id.strip()
    ]

except ValueError as e:
    raise ValueError(f"failed to parse configuration. details: {e}") from e
