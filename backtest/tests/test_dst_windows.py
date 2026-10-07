"""Danish-time windows -> UTC, across the 2025/26 DST changes."""
from __future__ import annotations

from datetime import timedelta

import pytest

from calmbt.config import ConfigError, windows_utc
from calmbt.engine import window_bars
from conftest import frozen, hourly_bars, make_cfg, utc


def one(start, end):
    cfg = make_cfg([{"name": "w", "start": start, "end": end}])
    return windows_utc(frozen(cfg))[0], cfg


def test_midnight_winter_is_23_utc_previous_day():
    w, _ = one("2025-01-15T00:00:00", "2025-01-16T00:00:00")
    assert w.start_utc == utc(2025, 1, 14, 23)
    assert w.end_utc == utc(2025, 1, 15, 23)


def test_midnight_summer_is_22_utc_previous_day():
    w, _ = one("2025-07-15T00:00:00", "2025-07-16T00:00:00")
    assert w.start_utc == utc(2025, 7, 14, 22)
    assert w.end_utc == utc(2025, 7, 15, 22)


@pytest.mark.parametrize("day,offset", [
    ("2025-03-29", 1), ("2025-03-30", 1),   # midnight on the change day is still winter time
    ("2025-03-31", 2),
    ("2025-10-25", 2), ("2025-10-26", 2),   # midnight on the change day is still summer time
    ("2025-10-27", 1),
    ("2026-03-28", 1), ("2026-03-29", 1), ("2026-03-30", 2),
])
def test_midnight_offset_around_each_change(day, offset):
    from datetime import datetime
    d = datetime.fromisoformat(day)
    nxt = (d + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    w, _ = one(day + "T00:00:00", nxt)
    assert w.start_utc == utc(d.year, d.month, d.day) - timedelta(hours=offset)


@pytest.mark.parametrize("start,end,hours", [
    ("2025-03-30T00:00:00", "2025-03-31T00:00:00", 23),  # spring forward: short day
    ("2025-10-26T00:00:00", "2025-10-27T00:00:00", 25),  # fall back: long day
    ("2026-03-29T00:00:00", "2026-03-30T00:00:00", 23),
    ("2025-03-29T00:00:00", "2025-03-31T00:00:00", 47),
    ("2025-10-25T00:00:00", "2025-10-27T00:00:00", 49),
    ("2025-03-30T00:00:00", "2025-03-30T12:00:00", 11),
    ("2025-10-26T00:00:00", "2025-10-26T12:00:00", 13),
    ("2025-12-01T00:00:00", "2025-12-02T00:00:00", 24),  # control: no change
    ("2025-07-01T00:00:00", "2025-07-02T00:00:00", 24),
])
def test_window_length_in_utc(start, end, hours):
    w, _ = one(start, end)
    assert w.end_utc - w.start_utc == timedelta(hours=hours)


def test_calendar_month_windows_across_changes():
    w, _ = one("2025-03-01T00:00:00", "2025-04-01T00:00:00")
    assert w.end_utc - w.start_utc == timedelta(days=31) - timedelta(hours=1)
    w, _ = one("2025-10-01T00:00:00", "2025-11-01T00:00:00")
    assert w.end_utc - w.start_utc == timedelta(days=31) + timedelta(hours=1)
    w, _ = one("2026-03-01T00:00:00", "2026-04-01T00:00:00")
    assert w.end_utc - w.start_utc == timedelta(days=31) - timedelta(hours=1)


def test_window_bar_counts_match_utc_length():
    for start, end, hours in [("2025-03-30T00:00:00", "2025-03-31T00:00:00", 23),
                              ("2025-10-26T00:00:00", "2025-10-27T00:00:00", 25),
                              ("2026-03-29T00:00:00", "2026-03-30T00:00:00", 23)]:
        w, cfg = one(start, end)
        bars = hourly_bars(w.start_utc - timedelta(hours=5), hours + 10)
        inside = window_bars(bars, w, timedelta(hours=1))
        assert len(inside) == hours
        assert inside[0].ts_utc == w.start_utc
        assert inside[-1].ts_utc + timedelta(hours=1) == w.end_utc


def test_local_clock_in_bars_skips_and_repeats_hour():
    # 2025-03-30 02:00 Danish does not exist; 2025-10-26 02:00 occurs twice.
    w, _ = one("2025-03-30T00:00:00", "2025-03-31T00:00:00")
    locals_ = [b.ts_dk.strftime("%H") for b in window_bars(hourly_bars(w.start_utc, 23), w, timedelta(hours=1))]
    assert "02" not in locals_ and len(locals_) == 23
    w, _ = one("2025-10-26T00:00:00", "2025-10-27T00:00:00")
    locals_ = [b.ts_dk.strftime("%H") for b in window_bars(hourly_bars(w.start_utc, 25), w, timedelta(hours=1))]
    assert locals_.count("02") == 2 and len(locals_) == 25


@pytest.mark.parametrize("bad", ["2025-03-30T02:30:00", "2026-03-29T02:00:00"])
def test_nonexistent_local_time_rejected(bad):
    with pytest.raises(ConfigError):
        one(bad, "2026-12-01T00:00:00")


def test_ambiguous_local_time_rejected():
    with pytest.raises(ConfigError):
        one("2025-10-26T02:30:00", "2025-10-27T00:00:00")


def test_offset_in_window_text_rejected():
    with pytest.raises(ConfigError):
        one("2025-01-01T00:00:00+01:00", "2025-01-02T00:00:00")


def test_local_fields_keep_danish_offset():
    w, _ = one("2025-10-26T00:00:00", "2025-10-27T00:00:00")
    assert w.start_local.utcoffset() == timedelta(hours=2)
    assert w.end_local.utcoffset() == timedelta(hours=1)
