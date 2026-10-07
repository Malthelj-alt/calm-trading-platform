"""The five agents, run in this order on every bar:

    DataAgent -> StrategyAgent -> RiskAgent -> PaperExecutionAgent -> JournalAgent

Nothing in here can place a real order. PaperExecutionAgent only computes
simulated fills from historical bar prices; there is no network, broker or
authentication code anywhere in this package.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

from .ledger import Ledger, compute_ledger_hash

LONG = "long"
FLAT = "flat"
SIGNALS = (LONG, FLAT)
ZERO = Decimal("0")


# =========================================================================== data
@dataclass(frozen=True)
class Bar:
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class History:
    """Read-only view of bars 0..t (inclusive). Bars after t are not reachable."""

    __slots__ = ("bars", "closes", "highs", "lows", "opens")

    def __init__(self, bars: Tuple[Bar, ...], opens: List[float], highs: List[float],
                 lows: List[float], closes: List[float]):
        self.bars = bars
        self.opens = opens
        self.highs = highs
        self.lows = lows
        self.closes = closes

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def t(self) -> int:
        return len(self.bars) - 1

    @property
    def last(self) -> Bar:
        return self.bars[-1]


def _parse_row(row: Sequence[str]) -> Optional[Bar]:
    if len(row) < 6:
        return None
    first = row[0].strip()
    if not first or not first.lstrip("-").isdigit():
        return None  # header or blank line
    return Bar(int(first), Decimal(row[1].strip()), Decimal(row[2].strip()),
               Decimal(row[3].strip()), Decimal(row[4].strip()), Decimal(row[5].strip()))


def load_bars(csv_path: str) -> List[Bar]:
    """Read open_time_ms,open,high,low,close,volume (header row optional)."""
    bars: List[Bar] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            bar = _parse_row(row)
            if bar is not None:
                bars.append(bar)
    for a, b in zip(bars, bars[1:]):
        if b.open_time_ms <= a.open_time_ms:
            raise ValueError("bars must be strictly increasing in time (at %d)" % b.open_time_ms)
    return bars


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def bars_sha256(bars: Sequence[Bar]) -> str:
    h = hashlib.sha256()
    for b in bars:
        h.update(("%d,%s,%s,%s,%s,%s\n" % (b.open_time_ms, b.open, b.high, b.low,
                                           b.close, b.volume)).encode("ascii"))
    return h.hexdigest()


def read_data_source(csv_path: str) -> str:
    """Source label from the sidecar <name>.meta.json written by the data scripts."""
    base, _ = os.path.splitext(csv_path)
    meta_path = base + ".meta.json"
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            for key in ("source_label", "source", "label", "data_source"):
                val = meta.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        except (OSError, ValueError):
            pass
    if "sample" in os.path.basename(csv_path).lower():
        return "sample (not real market)"
    return "unknown"


class DataAgent:
    """Agent 1. Owns the bars and hands out history only up to the clock t."""

    def __init__(self, bars: Sequence[Bar], source: str = "unknown", data_sha256: str = ""):
        self._bars: Tuple[Bar, ...] = tuple(bars)
        self._opens = [float(b.open) for b in self._bars]
        self._highs = [float(b.high) for b in self._bars]
        self._lows = [float(b.low) for b in self._bars]
        self._closes = [float(b.close) for b in self._bars]
        self.source = source
        self.data_sha256 = data_sha256 or bars_sha256(self._bars)

    @classmethod
    def from_csv(cls, csv_path: str) -> "DataAgent":
        return cls(load_bars(csv_path), read_data_source(csv_path), file_sha256(csv_path))

    def __len__(self) -> int:
        return len(self._bars)

    def time_ms(self, t: int) -> int:
        return self._bars[t].open_time_ms

    def history(self, t: int) -> History:
        """Bars 0..t inclusive. Nothing after t is included."""
        if t < 0 or t >= len(self._bars):
            raise IndexError(t)
        end = t + 1
        return History(self._bars[:end], self._opens[:end], self._highs[:end],
                       self._lows[:end], self._closes[:end])

    def stream(self, start: int, stop: Optional[int] = None) -> Iterator[Tuple[int, History]]:
        """Advance the clock bar by bar, yielding (t, bars up to t)."""
        stop = len(self._bars) if stop is None else stop
        for t in range(start, stop):
            yield t, self.history(t)


# =========================================================================== strategy
class StrategyAgent:
    """Agent 2. Wraps one pure strategy function: signal(history) -> 'long' | 'flat'."""

    def __init__(self, fn: Callable[[History], str], name: str = ""):
        self.fn = fn
        self.name = name or getattr(fn, "__name__", "strategy")

    def decide(self, history: History) -> str:
        sig = self.fn(history)
        if sig not in SIGNALS:
            raise ValueError("strategy %s returned %r, expected 'long' or 'flat'" % (self.name, sig))
        return sig


# =========================================================================== risk
@dataclass(frozen=True)
class Order:
    side: str                 # 'buy' | 'sell'
    notional: Decimal         # buy: quote amount committed (incl. fee); sell: qty * ref price
    qty: Decimal              # sell: base qty to sell; buy: 0 (decided at fill)
    signal_index: int
    signal_time_ms: int


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    order: Optional[Order]
    reason: str               # 'buy' | 'sell' | 'hold' | 'below_min_notional' | ...


class RiskAgent:
    """Agent 3. 100 % sizing, long only, no leverage, minimum $5 notional."""

    MIN_NOTIONAL = Decimal("5")

    def __init__(self, min_notional: Decimal = MIN_NOTIONAL):
        self.min_notional = Decimal(min_notional)

    def notional_ok(self, notional: Decimal) -> bool:
        return Decimal(notional) >= self.min_notional

    def review(self, signal: str, cash: Decimal, position_qty: Decimal, ref_price: Decimal,
               signal_index: int = 0, signal_time_ms: int = 0) -> RiskDecision:
        if signal not in SIGNALS:
            raise ValueError("unknown signal %r" % (signal,))
        cash = Decimal(cash)
        position_qty = Decimal(position_qty)
        if cash < ZERO or position_qty < ZERO:
            return RiskDecision(False, None, "invalid_state")
        if signal == LONG:
            if position_qty > ZERO:
                return RiskDecision(False, None, "hold")
            notional = cash  # 100 % of the account, never more: no leverage
            if not self.notional_ok(notional):
                return RiskDecision(False, None, "below_min_notional")
            return RiskDecision(True, Order("buy", notional, ZERO, signal_index, signal_time_ms), "buy")
        # FLAT: long only, so the most we can ever sell is what we hold.
        if position_qty == ZERO:
            return RiskDecision(False, None, "hold")
        notional = position_qty * Decimal(ref_price)
        if not self.notional_ok(notional):
            return RiskDecision(False, None, "below_min_notional")
        return RiskDecision(True, Order("sell", notional, position_qty, signal_index, signal_time_ms), "sell")


# =========================================================================== execution
@dataclass(frozen=True)
class Fill:
    side: str
    reason: str               # 'signal' | 'final_close'
    signal_index: int
    signal_time_ms: int
    fill_index: int
    fill_time_ms: int
    ref_price: Decimal        # raw bar price used (next-bar open, or last close)
    price: Decimal            # after slippage
    qty: Decimal
    notional: Decimal         # gross quote value; fee = fee_rate * notional
    fee: Decimal
    cash_delta: Decimal       # change in quote balance
    qty_delta: Decimal        # change in base position


class PaperExecutionAgent:
    """Agent 4. Simulated fills only.

    Buy at open[t+1]*(1+s): fee = f*C, qty = (C - fee) / price.
    Sell at open[t+1]*(1-s): gross = qty*price, fee = f*gross, cash += gross - fee.
    Final bar: any open position closes at close*(1-s), fee f -> qty*close*(1-s)*(1-f).
    """

    def __init__(self, fee_rate: Decimal, slippage_rate: Decimal):
        self.f = Decimal(fee_rate)
        self.s = Decimal(slippage_rate)

    def fill_next_open(self, order: Order, bar: Bar, bar_index: int) -> Fill:
        if bar_index != order.signal_index + 1:
            raise ValueError("fills must happen at t+1 (signal %d, fill %d)"
                             % (order.signal_index, bar_index))
        if order.side == "buy":
            price = bar.open * (1 + self.s)
            fee = order.notional * self.f
            qty = (order.notional - fee) / price
            return Fill("buy", "signal", order.signal_index, order.signal_time_ms, bar_index,
                        bar.open_time_ms, bar.open, price, qty, order.notional, fee,
                        -order.notional, qty)
        if order.side == "sell":
            price = bar.open * (1 - self.s)
            gross = order.qty * price
            fee = gross * self.f
            return Fill("sell", "signal", order.signal_index, order.signal_time_ms, bar_index,
                        bar.open_time_ms, bar.open, price, order.qty, gross, fee,
                        gross - fee, -order.qty)
        raise ValueError("unknown side %r" % order.side)

    def close_at_end(self, qty: Decimal, bar: Bar, bar_index: int) -> Fill:
        price = bar.close * (1 - self.s)
        gross = qty * price
        fee = gross * self.f
        return Fill("sell", "final_close", bar_index, bar.open_time_ms, bar_index,
                    bar.open_time_ms, bar.close, price, qty, gross, fee, gross - fee, -qty)


# =========================================================================== journal
class JournalAgent:
    """Agent 5. Appends fills and closed trades to the SQLite ledger and hashes it."""

    def __init__(self, ledger: Ledger, run_id: int, data_source: str):
        self.ledger = ledger
        self.run_id = run_id
        self.data_source = data_source
        self._fill_seq = {}
        self._trade_seq = {}

    def record_fill(self, test_id: int, fill: Fill, cash_after: Decimal,
                    position_after: Decimal) -> int:
        seq = self._fill_seq.get(test_id, 0) + 1
        self._fill_seq[test_id] = seq
        return self.ledger.append_fill(self.run_id, test_id, {
            "seq": seq, "side": fill.side, "reason": fill.reason,
            "signal_index": fill.signal_index, "signal_time_ms": fill.signal_time_ms,
            "fill_index": fill.fill_index, "fill_time_ms": fill.fill_time_ms,
            "ref_price": fill.ref_price, "price": fill.price, "qty": fill.qty,
            "notional": fill.notional, "fee": fill.fee, "cash_after": cash_after,
            "position_after": position_after, "data_source": self.data_source,
        })

    def record_trade(self, test_id: int, trade: dict) -> int:
        seq = self._trade_seq.get(test_id, 0) + 1
        self._trade_seq[test_id] = seq
        row = dict(trade)
        row["seq"] = seq
        row["data_source"] = self.data_source
        return self.ledger.append_trade(self.run_id, test_id, row)

    def finish_test(self, test_id: int, stats: dict) -> None:
        self.ledger.finalize_test(test_id, stats)

    def ledger_hash(self) -> str:
        return compute_ledger_hash(self.ledger.conn, self.run_id)
