from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLEAR_TOKENS = {"none", "never", "clear", "-", "off", "remove"}

_REL_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_REL_RE = re.compile(r"^\s*in\s+|^\s*\+\s*")
_REL_PIECE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])(?![a-z])", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_SHORT_DATE_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4,
    "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_DAY_WORDS = {"today", "tonight", "tomorrow"}

EXAMPLES = (
    "30m, 2h, 1d6h, 15:00, 3:30pm, today 15:00, tonight, tomorrow 9am, "
    "friday, fri 3pm, 2026-10-24 15:00, 10-24"
)


class DueError(ValueError):
    def __init__(self, message: str):
        super().__init__(f"{message} try one of: {EXAMPLES}")


def timezone() -> ZoneInfo:
    name = (os.getenv("LEECH_TZ") or "UTC").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def is_clear_token(text: str) -> bool:
    return (text or "").strip().strip('"').strip("'").lower() in CLEAR_TOKENS


@dataclass
class Due:
    epoch: float
    human: str

    @property
    def relative(self) -> str:
        return humanize(self.epoch - time.time())


def _strip(text: str) -> str:
    return (text or "").strip().strip('"').strip("'").strip()


def _mk(hour: int, minute: int, *, base_day, advance_day: int = 0) -> float:
    import datetime as dt

    day = base_day + dt.timedelta(days=advance_day)
    local = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=timezone())
    return local.timestamp()


def _apply_clock(
    base_day, text: str, *, default_hour=None, what: str, advance_day: int = 0
):
    import datetime as dt

    piece = text.strip()
    if not piece:
        if default_hour is None:
            raise DueError(f"{what} needs a time, like '{what} 15:00'.")
        return _mk(default_hour, 0, base_day=base_day, advance_day=advance_day)
    match = _CLOCK_RE.match(piece)
    if not match:
        raise DueError(f"'{piece}' is not a readable time.")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if minute > 59 or hour > 23:
        raise DueError(f"'{piece}' is not a real time.")
    if meridiem:
        if hour < 1 or hour > 12:
            raise DueError(f"'{piece}' is not a real 12-hour time.")
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    return _mk(hour, minute, base_day=base_day, advance_day=advance_day)


def _parse_clock_today(text: str, now: float) -> float:
    import datetime as dt

    today = dt.datetime.fromtimestamp(now, tz=timezone()).date()
    stamp = _apply_clock(today, text, default_hour=None, what="that")
    if stamp <= now:  # already past: assume the next day
        stamp += 86400
    return stamp


def _parse_relative(text: str, now: float) -> float:
    body = _REL_RE.sub("", text).strip().replace(" ", "")
    total = 0.0
    seen = ""
    leftovers = body
    for number, unit in _REL_PIECE.findall(body):
        total += float(number) * _REL_UNIT[unit.lower()]
        seen += f"{number}{unit}"
        leftovers = leftovers.replace(f"{number}{unit}", "", 1)
    if not seen or leftovers:
        raise DueError(f"'{text}' has no readable duration in it.")
    if total <= 0:
        raise DueError("due time is in the past. leech is good but not that good 😟")
    return now + total


def _parse_weekday(word: str, rest: str, now: float) -> float:
    import datetime as dt

    target = _WEEKDAYS[word]
    today = dt.datetime.fromtimestamp(now, tz=timezone()).date()
    delta = (target - today.weekday()) % 7
    stamp = _apply_clock(
        today + dt.timedelta(days=delta), rest, default_hour=9, what=word
    )
    if stamp <= now: 
        stamp += 7 * 86400
    return stamp


def _parse_date(text: str, rest: str, now: float) -> float:
    import datetime as dt

    today = dt.datetime.fromtimestamp(now, tz=timezone()).date()
    if _DATE_RE.match(text):
        year, month, day = (int(part) for part in text.split("-"))
        try:
            base = dt.date(year, month, day)
        except ValueError:
            raise DueError(f"'{text}' is not a real date.")
    else:
        month, day = (int(part) for part in text.split("-"))
        try:
            base = dt.date(today.year, month, day)
        except ValueError:
            raise DueError(f"'{text}' is not a real date.")
        if dt.datetime.combine(
            base, dt.time(23, 59), tzinfo=timezone()
        ).timestamp() <= now:
            base = dt.date(today.year + 1, month, day)
    stamp = _apply_clock(base, rest, default_hour=9, what="that date")
    return stamp


def parse_due(text: str, *, now: float | None = None) -> Due:
    import datetime as dt

    text = _strip(text)
    if not text:
        raise DueError("empty due time.")
    now = time.time() if now is None else now
    lowered = text.lower()

    if lowered in ("now", "asap"):
        return Due(now + 60, render(now + 60))

    if lowered in _DAY_WORDS:
        today = dt.datetime.fromtimestamp(now, tz=timezone()).date()
        if lowered == "today":
            stamp = _apply_clock(today, "", default_hour=None, what="today")
        elif lowered == "tonight":
            stamp = _apply_clock(today, "", default_hour=20, what="tonight")
        else:  # tomorrow
            stamp = _apply_clock(
                today, "", default_hour=9, what="tomorrow", advance_day=1
            )
        if stamp <= now:
            raise DueError(f"'{text}' is already in the past.")
        return Due(stamp, render(stamp))

    head, _, rest = lowered.partition(" ")
    if head in _DAY_WORDS and rest:
        today = dt.datetime.fromtimestamp(now, tz=timezone()).date()
        advance = 1 if head == "tomorrow" else 0
        stamp = _apply_clock(
            today, rest, default_hour=None, what=head, advance_day=advance
        )
        if stamp <= now:
            raise DueError(f"'{text}' is already in the past.")
        return Due(stamp, render(stamp))

    if head in _WEEKDAYS:
        return Due(*_with_human(_parse_weekday(head, rest, now)))

    if _REL_PIECE.match(_REL_RE.sub("", lowered).replace(" ", "")) or lowered.startswith("in "):
        epoch = _parse_relative(lowered, now)
        return Due(epoch, render(epoch))

    if _CLOCK_RE.match(lowered):
        epoch = _parse_clock_today(lowered, now)
        return Due(epoch, render(epoch))

    if _DATE_RE.match(head) or _SHORT_DATE_RE.match(head):
        return Due(*_with_human(_parse_date(head, rest, now)))

    raise DueError(f"'{text}' is not a due time i understand.")


def _with_human(epoch: float) -> tuple[float, str]:
    return epoch, render(epoch)


def parse_snooze(text: str, *, now: float | None = None) -> float:
    text = _strip(text)
    now = time.time() if now is None else now
    lowered = text.lower()
    if lowered in ("snooze", "later", "+", "snooze me"):
        return now + 3600  # bare snooze = one hour
    if lowered.startswith("snooze ") or lowered.startswith("+"):
        return parse_snooze(lowered.split(" ", 1)[-1].lstrip("+"), now=now)
    due = parse_due(lowered, now=now)
    if due.epoch <= now:
        raise DueError("snoozing into the past?")
    return due.epoch


def humanize(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    parts = []
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        count, seconds = divmod(seconds, size)
        if count and len(parts) < 2:
            parts.append(f"{count}{unit}")
    return " ".join(parts) if parts else "0s"


def render(epoch: float) -> str:
    import datetime as dt

    local = dt.datetime.fromtimestamp(epoch, tz=timezone())
    return local.strftime("%a %m-%d %H:%M")


def format_due(due: float | None, *, now: float | None = None) -> str:
    if due is None:
        return ""
    now = time.time() if now is None else now
    delta = due - now
    if delta < 0:
        return f"⚠️ overdue by {humanize(-delta)}"
    if delta < 86400:
        return f"⏰ in {humanize(delta)}"
    return f"⏰ {render(due)} (in {humanize(delta)})"
