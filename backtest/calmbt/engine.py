"""Window engine: same cash per window, next-open fills, forced close-out at window end."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, List, Mapping, Sequence

from .config import Window


@dataclass
class WindowResult:
    strategy: str
    window: str
    start_dkk: float          # cash in DKK at window start
    end_dkk: float            # cash in DKK at window end (== final value)
    pnl_dkk: float
    return_pct: float
    fees_dkk: float
    trades: int
    bars: int
    final_position: float     # units held after window end, must be 0
    final_cash_dkk: float
    final_value_dkk: float
    status: str = "OK"
    fills: List[dict] = field(default_factory=list)


def window_bars(bars: Sequence[Any], w: Window, interval: timedelta) -> List[Any]:
    """Bars that open at/after window start and CLOSE (open+interval) at/before window end."""
    return [b for b in bars if b.ts_utc >= w.start_utc and b.ts_utc + interval <= w.end_utc]


def run_window(strategy: Any, bars: Sequence[Any], w: Window, cfg: Mapping[str, Any],
               interval: timedelta) -> WindowResult:
    fx = float(cfg["fx_to_dkk"])
    fee = float(cfg["fee_bps"]) / 1e4
    slip = float(cfg["slippage_bps"]) / 1e4
    start_dkk = float(cfg["starting_capital_dkk"])
    cash = start_dkk / fx  # quote currency
    units = 0.0
    fees = 0.0
    trades = 0
    fills: List[dict] = []
    wb = window_bars(bars, w, interval)
    strategy.reset()

    def buy(price: float, ts: datetime) -> None:
        nonlocal cash, units, fees, trades
        px = price * (1 + slip)
        q = cash / (px * (1 + fee))
        f = q * px * fee
        units, cash, fees, trades = q, 0.0, fees + f, trades + 1
        fills.append({"ts_utc": ts.isoformat(), "side": "buy", "price": px, "units": q, "fee_quote": f})

    def sell(price: float, ts: datetime, reason: str) -> None:
        nonlocal cash, units, fees, trades
        px = price * (1 - slip)
        gross = units * px
        f = gross * fee
        fills.append({"ts_utc": ts.isoformat(), "side": "sell", "price": px, "units": units,
                      "fee_quote": f, "reason": reason})
        cash, units, fees, trades = gross - f, 0.0, fees + f, trades + 1

    if not wb:
        return WindowResult(strategy.name, w.name, start_dkk, start_dkk, 0.0, 0.0, 0.0, 0, 0,
                            0.0, start_dkk, start_dkk, status="NO_DATA")

    pending = int(getattr(strategy, "initial_target", 0))  # decision to act on at next open
    seen: List[Any] = []
    last = len(wb) - 1
    for i, bar in enumerate(wb):
        # 1) fill the pending decision at this bar's OPEN
        if pending == 1 and units == 0:
            buy(bar.open, bar.ts_utc)
        elif pending == 0 and units > 0:
            sell(bar.open, bar.ts_utc, "signal")
        # 2) forced liquidation at the CLOSE of the last bar in the window
        if i == last:
            if units > 0:
                sell(bar.close, bar.ts_utc, "window_end")
            break
        # 3) decide at this bar's close (history = strictly earlier bars of this window)
        pending = int(strategy.on_bar(bar, 1 if units > 0 else 0, seen))
        seen.append(bar)

    end_dkk = cash * fx
    pnl = end_dkk - start_dkk
    return WindowResult(
        strategy.name, w.name, start_dkk, end_dkk, pnl, pnl / start_dkk * 100.0,
        fees * fx, trades, len(wb), units, cash * fx, (cash + units * wb[-1].close) * fx, fills=fills)
