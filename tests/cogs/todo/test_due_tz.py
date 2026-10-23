import pytest

from leech.cogs.todo import due as due_mod
from leech.cogs.todo.due import parse_due, render, format_due, parse_snooze


NOW = 1_800_000_000.0


def test_same_time_different_tz_offsets():
    utc = parse_due("15:00", now=NOW).epoch
    dam = parse_due("15:00", now=NOW, tz="Asia/Damascus").epoch
    # damascus is utc+3 in october: same wall clock is 3h earlier epoch
    assert abs((utc - dam) - 3 * 3600) < 1


def test_none_falls_back_to_leech_tz(monkeypatch):
    monkeypatch.setenv("LEECH_TZ", "Asia/Damascus")
    a = parse_due("15:00", now=NOW).epoch
    b = parse_due("15:00", now=NOW, tz=None).epoch
    assert a == b


def test_render_uses_tz():
    a = parse_due("15:00", now=NOW, tz="UTC").epoch
    assert render(a, tz="UTC").endswith("15:00")
    assert render(a, tz="Asia/Damascus").endswith("18:00")


def test_format_due_far_uses_day_emote():
    a = parse_due("15:00", now=NOW, tz="UTC").epoch + 5 * 86400
    text = format_due(a, now=NOW)
    assert text.startswith("📅") or text.startswith("⏰")


def test_format_due_soon_uses_clock_emote():
    text = format_due(NOW + 3600, now=NOW)
    assert text.startswith("⏰ in 1h") or text == "⏰ in 1h"


def test_format_due_overdue():
    text = format_due(NOW - 60, now=NOW)
    assert text.startswith("⚠️ overdue")


def test_snooze_respects_tz():
    when = parse_snooze("tomorrow 9am", now=NOW, tz="Asia/Damascus")
    again = parse_due("tomorrow 9am", now=NOW, tz="Asia/Damascus").epoch
    assert when == again


def test_valid_tz_and_rejects_garbage():
    assert due_mod.valid_tz("Asia/Damascus").key == "Asia/Damascus"
    with pytest.raises(ValueError):
        due_mod.valid_tz("Mars/Olympus")
