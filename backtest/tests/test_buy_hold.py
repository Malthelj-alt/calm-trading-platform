"""Buy-and-hold: enter at first open, forced exit at last close inside the window."""
from __future__ import annotations

from datetime import timedelta

import pytest

from calmbt.config import interval_timedelta, windows_utc
from calmbt.engine import run_window
from calmbt.strategies import BuyHold
from conftest import frozen, hourly_bars, make_cfg, utc, wiggly_price
from calmbt import data as dmod

H = timedelta(hours=1)


def setup(start="2025-01-10T00:00:00", end="2025-01-11T00:00:00", **over):
    cfg = make_cfg([{"name": "w", "start": start, "end": end}], **over)
    return cfg, windows_utc(frozen(cfg))[0]


def run(bars, cfg, w):
    return run_window(BuyHold(), bars, w, cfg, interval_timedelta(cfg["bar_interval"]))


def test_enters_at_first_open_exits_at_last_close():
    cfg, w = setup()
    bars = hourly_bars(w.start_utc - 3 * H, 40)          # bars before/after the window too
    inside = [b for b in bars if w.start_utc <= b.ts_utc < w.end_utc]
    r = run(bars, cfg, w)
    assert [f["side"] for f in r.fills] == ["buy", "sell"]
    buy, sell = r.fills
    assert buy["ts_utc"] == inside[0].ts_utc.isoformat() and buy["price"] == inside[0].open
    assert sell["ts_utc"] == inside[-1].ts_utc.isoformat() and sell["price"] == inside[-1].close
    assert sell["reason"] == "window_end"
    assert r.trades == 2


def test_gap_at_window_end_does_not_fill_at_next_open():
    cfg, w = setup()
    # window has 24 bars; drop the last 4 (data gap) and put a crazy bar after the window end
    full = hourly_bars(w.start_utc, 24)
    kept = full[:20]
    after = hourly_bars(w.end_utc, 3, lambda i: 9999.0)
    r = run(kept + after, cfg, w)
    sell = r.fills[-1]
    assert sell["price"] == kept[-1].close
    assert sell["ts_utc"] == kept[-1].ts_utc.isoformat()
    assert all(f["price"] < 9000 for f in r.fills)
    assert r.final_position == 0


def test_gap_in_middle_of_window_ok_and_empty_tail_after_window_ignored():
    cfg, w = setup()
    full = hourly_bars(w.start_utc, 24)
    bars = full[:5] + full[10:]          # hole in the middle
    r = run(bars, cfg, w)
    assert r.fills[0]["price"] == bars[0].open
    assert r.fills[-1]["price"] == bars[-1].close


def test_bar_straddling_window_end_is_not_used():
    # window ends 12:30 Danish: the 12:00 bar closes at 13:00 (after the end) so it is excluded.
    cfg, w = setup("2025-01-10T00:00:00", "2025-01-10T12:30:00")
    bars = hourly_bars(w.start_utc, 20)
    r = run(bars, cfg, w)
    last_inside = [b for b in bars if b.ts_utc + H <= w.end_utc][-1]
    assert last_inside.ts_dk.hour == 11
    assert r.fills[-1]["price"] == last_inside.close
    assert r.bars == 12


def test_no_data_window_stays_in_cash():
    cfg, w = setup()
    r = run(hourly_bars(w.end_utc + H, 5), cfg, w)
    assert r.status == "NO_DATA" and r.trades == 0 and r.final_position == 0
    assert r.end_dkk == r.start_dkk


def test_fees_charged_on_both_trades_zero_slippage():
    cfg, w = setup(fee_bps=10.0)
    bars = hourly_bars(w.start_utc, 24, lambda i: 100.0 + i)
    r = run(bars, cfg, w)
    buy, sell = r.fills
    fee = 0.001
    assert buy["fee_quote"] > 0 and sell["fee_quote"] > 0
    units = 100000.0 / (bars[0].open * (1 + fee))
    assert buy["units"] == pytest.approx(units)
    assert buy["fee_quote"] == pytest.approx(units * bars[0].open * fee)
    assert sell["fee_quote"] == pytest.approx(units * bars[-1].close * fee)
    expected_end = units * bars[-1].close * (1 - fee)
    assert r.end_dkk == pytest.approx(expected_end)
    assert r.fees_dkk == pytest.approx(buy["fee_quote"] + sell["fee_quote"])
    # total cost is cheaper than no fees by about both legs
    cfg0, _ = setup(fee_bps=0.0)
    r0 = run(bars, cfg0, w)
    assert r0.end_dkk > r.end_dkk and r0.fees_dkk == 0


def test_slippage_and_fx_applied_to_both_legs():
    cfg, w = setup(fee_bps=0.0, slippage_bps=10.0, fx_to_dkk=7.0)
    bars = hourly_bars(w.start_utc, 24)
    r = run(bars, cfg, w)
    buy, sell = r.fills
    assert buy["price"] == pytest.approx(bars[0].open * 1.001)
    assert sell["price"] == pytest.approx(bars[-1].close * 0.999)
    assert r.start_dkk == 100000.0
    assert r.end_dkk == pytest.approx(100000.0 / 7.0 / buy["price"] * sell["price"] * 7.0)


def test_final_value_equals_cash_and_nothing_held():
    cfg, w = setup(fee_bps=10.0, slippage_bps=5.0)
    r = run(hourly_bars(w.start_utc, 30, wiggly_price), cfg, w)
    assert r.final_position == 0
    assert r.final_value_dkk == r.final_cash_dkk == r.end_dkk
    assert r.pnl_dkk == pytest.approx(r.end_dkk - r.start_dkk)


def test_single_bar_window_buys_open_sells_close():
    cfg, w = setup("2025-01-10T00:00:00", "2025-01-10T01:00:00", fee_bps=5.0)
    bars = hourly_bars(w.start_utc, 3)
    r = run(bars, cfg, w)
    assert [f["side"] for f in r.fills] == ["buy", "sell"]
    assert r.fills[0]["price"] == bars[0].open and r.fills[1]["price"] == bars[0].close
    assert r.final_position == 0


def test_dst_long_day_still_ends_flat():
    cfg, w = setup("2025-10-26T00:00:00", "2025-10-27T00:00:00", fee_bps=10.0)
    bars = hourly_bars(w.start_utc, 26)
    r = run(bars, cfg, w)
    assert r.bars == 25
    assert r.fills[-1]["ts_utc"] == bars[24].ts_utc.isoformat()
    assert r.final_position == 0
