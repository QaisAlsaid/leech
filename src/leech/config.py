import os, re
from importlib.metadata import PackageNotFoundError, version as _pkg_version

_owner_id_str = os.getenv("OWNER_ID")
_bot_id_str = os.getenv("BOT_ID")

if not _bot_id_str:
    raise ValueError("failed to load 'BOT_ID' environment variable.")
if not _owner_id_str:
    raise ValueError("failed to load 'OWNER_ID' environment variable.")

try:
    bot_id: int = int(_bot_id_str)
    owner_ids: list[int] = [
        int(owner.strip()) for owner in _owner_id_str.split(",") if owner.strip()
    ]
except ValueError as e:
    raise ValueError(f"failed to parse configuration. details: {e}") from e

AI_PROVIDERS = {"gemini": "AI_API_KEY", "groq": "GROQ_API_KEY"}

_chain_str = os.getenv(
    "AI_CHAIN", "groq:openai/gpt-oss-120b,gemini:gemini-3.5-flash-lite"
)
ai_thinking: str = os.getenv("AI_THINKING", "low").lower()  # low | high | off
ai_read_timeout: float = float(os.getenv("AI_TIMEOUT", "20"))
ai_turn_deadline: float = float(os.getenv("AI_TURN_DEADLINE", "75"))
ai_chain: list[tuple[str, str]] = []
for entry in _chain_str.split(","):
    entry = entry.strip()
    if not entry:
        continue
    provider, _, model = entry.partition(":")
    provider, model = provider.strip(), model.strip()
    if provider not in AI_PROVIDERS:
        raise ValueError(
            f"unknown ai provider: '{provider}' in AI_CHAIN (known: {', '.join(AI_PROVIDERS)})."
        )
    if not model:
        raise ValueError(f"missing model for provider '{provider}' in AI_CHAIN.")
    ai_chain.append((provider, model))

ai_keys: dict[str, str] = {}
for provider, env in AI_PROVIDERS.items():
    key = os.getenv(env)
    if key:
        ai_keys[provider] = key
    elif any(p == provider for p, _ in ai_chain):
        raise ValueError(f"provider '{provider}' is in AI_CHAIN but {env} is not set.")

ai_window_limit: int = int(os.getenv("AI_WINDOW_LIMIT", "60"))
ai_window_budget: int = int(os.getenv("AI_WINDOW_BUDGET", "8000"))
prefixes: list[str] = [
    p.strip() for p in os.getenv("PREFIXES", ">, $").split(",") if p.strip()
]


def _tree_version() -> str:
    baked = (os.getenv("LEECH_GIT_DESCRIBE") or "").strip()
    if baked:
        return baked
    import subprocess

    for source_dir in ("src", "."):
        try:
            describe = subprocess.run(
                ["git", "-C", source_dir, "describe", "--tags", "--always", "--dirty"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if describe.returncode == 0 and describe.stdout.strip():
            out = describe.stdout.strip()
            if re.fullmatch(r"[0-9a-f]{7,40}", out):
                try:
                    return f"{_pkg_version('leech')} @ {out[:12]}"
                except PackageNotFoundError:
                    return out[:12]
            return out
    try:
        pkg = _pkg_version("leech")
    except PackageNotFoundError:
        pkg = ""
    describe = (os.getenv("LEECH_GIT_DESCRIBE") or "").strip()
    if not describe:
        import subprocess

        for source_dir in ("src", "."):
            try:
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        source_dir,
                        "describe",
                        "--tags",
                        "--always",
                        "--dirty",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0 and proc.stdout.strip():
                describe = proc.stdout.strip()
                break

    def _channel_suffix(out: str) -> str:
        channel = (os.getenv("LEECH_GIT_CHANNEL") or "").strip().lower()
        if channel in ("canary", "dev"):
            return f"{out} ({channel})"
        return out

    if not describe:
        return _channel_suffix(pkg) if pkg else "unknown"
    if re.fullmatch(r"[0-9a-f]{7,40}", describe):
        return _channel_suffix(f"{pkg} @ {describe[:12]}" if pkg else describe[:12])
    if pkg and pkg not in describe:
        return _channel_suffix(f"{pkg} - {describe}")
    return _channel_suffix(describe)


version_str: str = _tree_version()
