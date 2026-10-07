"""The ten strategies. Each one is a pure function: signal(history) -> 'long' | 'flat'.

``history`` is a :class:`tradebot.agents.History` holding only the bars closed up to
and including the decision bar t (``history.closes[-1]`` is the close of bar t).
A strategy never sees bar t+1 or anything later, and it keeps no state between
calls: hysteresis strategies (RSI, Bollinger, Donchian) rebuild their state by
scanning the history they were given, so the same history always gives the same
answer.

Indicator maths uses floats (IEEE-754, deterministic for identical inputs);
all money maths elsewhere uses Decimal.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, List, Sequence

LONG = "long"
FLAT = "flat"
SEED = 42


# --------------------------------------------------------------------------- helpers
def _sma_last(values: Sequence[float], n: int) -> float:
    return math.fsum(values[-n:]) / n


def _ema_series(values: Sequence[float], n: int) -> List[float]:
    """EMA seeded with the first value. Returns one value per input."""
    alpha = 2.0 / (n + 1.0)
    out: List[float] = []
    ema = values[0]
    for v in values:
        ema = alpha * v + (1.0 - alpha) * ema
        out.append(ema)
    return out


def _rsi_series(closes: Sequence[float], n: int = 14) -> List[float]:
    """Wilder RSI. Entries before the first full window are NaN."""
    out = [math.nan] * len(closes)
    if len(closes) <= n:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g = gains / n
    avg_l = losses / n

    def rsi(g: float, l: float) -> float:
        if l == 0.0:
            return 100.0 if g > 0.0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[n] = rsi(avg_g, avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = rsi(avg_g, avg_l)
    return out


# --------------------------------------------------------------------------- strategies
def buy_and_hold(history) -> str:
    return LONG


def _sma_cross(fast: int, slow: int) -> Callable:
    def signal(history) -> str:
        c = history.closes
        if len(c) < slow:
            return FLAT
        return LONG if _sma_last(c, fast) > _sma_last(c, slow) else FLAT

    signal.__name__ = "sma_cross_%d_%d" % (fast, slow)
    return signal


sma_cross_20_50 = _sma_cross(20, 50)
sma_cross_50_200 = _sma_cross(50, 200)


def ema_cross_12_26(history) -> str:
    c = history.closes
    if len(c) < 26:
        return FLAT
    return LONG if _ema_series(c, 12)[-1] > _ema_series(c, 26)[-1] else FLAT


def rsi_reversion(history) -> str:
    """Enter when RSI14 < 30, stay long until RSI14 > 55."""
    rsi = _rsi_series(history.closes, 14)
    state = FLAT
    for r in rsi:
        if math.isnan(r):
            continue
        if state == FLAT and r < 30.0:
            state = LONG
        elif state == LONG and r > 55.0:
            state = FLAT
    return state


def bollinger_reversion(history) -> str:
    """Enter when close < SMA20 - 2 sigma, exit when close >= SMA20."""
    c = history.closes
    n, k = 20, 2.0
    if len(c) < n:
        return FLAT
    state = FLAT
    # Rolling sums around a fixed anchor keep the variance numerically stable.
    anchor = c[0]
    s = 0.0
    sq = 0.0
    for x in c[:n]:
        d = x - anchor
        s += d
        sq += d * d
    for i in range(n - 1, len(c)):
        if i >= n:
            d_in = c[i] - anchor
            d_out = c[i - n] - anchor
            s += d_in - d_out
            sq += d_in * d_in - d_out * d_out
        mean_d = s / n
        mean = anchor + mean_d
        var = sq / n - mean_d * mean_d
        lower = mean - k * math.sqrt(var) if var > 0.0 else mean
        x = c[i]
        if state == FLAT and x < lower:
            state = LONG
        elif state == LONG and x >= mean:
            state = FLAT
    return state


def donchian_breakout(history) -> str:
    """Enter when close > highest high of the previous 20 bars,
    exit when close < lowest low of the previous 10 bars."""
    c, h, l = history.closes, history.highs, history.lows
    entry_n, exit_n = 20, 10
    if len(c) <= entry_n:
        return FLAT
    state = FLAT
    for i in range(entry_n, len(c)):
        if state == FLAT:
            if c[i] > max(h[i - entry_n:i]):
                state = LONG
        else:
            if c[i] < min(l[i - exit_n:i]):
                state = FLAT
    return state


def momentum_24(history) -> str:
    c = history.closes
    if len(c) <= 24:
        return FLAT
    return LONG if c[-1] / c[-25] - 1.0 > 0.0 else FLAT


def macd_12_26_9(history) -> str:
    c = history.closes
    if len(c) < 35:
        return FLAT
    fast = _ema_series(c, 12)
    slow = _ema_series(c, 26)
    macd = [a - b for a, b in zip(fast, slow)]
    sig = _ema_series(macd, 9)
    return LONG if macd[-1] > sig[-1] else FLAT


def random_control(history) -> str:
    """Seeded coin flip, re-tossed once per UTC day (every 24 bars).

    The coin depends only on (seed, day of the decision bar), so it is pure,
    replayable and blind to the future."""
    day = history.bars[-1].open_time_ms // 86_400_000
    rng = random.Random(SEED * 1_000_003 + day)
    return LONG if rng.random() < 0.5 else FLAT


# --------------------------------------------------------------------------- registry
@dataclass(frozen=True)
class StrategySpec:
    number: int
    name: str
    key: str
    description: str
    fn: Callable

    @property
    def test_name(self) -> str:
        return "%s %02d" % (self.name, self.number)


STRATEGIES: List[StrategySpec] = [
    StrategySpec(1, "Buy & Hold", "buy_hold", "Long from the first bar of the window to the last.", buy_and_hold),
    StrategySpec(2, "SMA Cross 20/50", "sma_20_50", "Long while SMA20 > SMA50.", sma_cross_20_50),
    StrategySpec(3, "SMA Cross 50/200", "sma_50_200", "Long while SMA50 > SMA200.", sma_cross_50_200),
    StrategySpec(4, "EMA Cross 12/26", "ema_12_26", "Long while EMA12 > EMA26.", ema_cross_12_26),
    StrategySpec(5, "RSI Reversion", "rsi_reversion", "Buy when RSI14 < 30, exit when RSI14 > 55.", rsi_reversion),
    StrategySpec(6, "Bollinger Reversion", "bollinger_reversion", "Buy below the lower 20-bar 2-sigma band, exit at the middle band.", bollinger_reversion),
    StrategySpec(7, "Donchian Breakout", "donchian_20_10", "Buy above the 20-bar high, exit below the 10-bar low.", donchian_breakout),
    StrategySpec(8, "Momentum", "momentum_24", "Long while the 24-bar return is above 0.", momentum_24),
    StrategySpec(9, "MACD", "macd_12_26_9", "Long while MACD(12,26) is above its 9-bar signal line.", macd_12_26_9),
    StrategySpec(10, "Random Control", "random_control", "Seeded coin flip (seed 42), re-tossed once per day.", random_control),
]

TEST_NAMES: List[str] = [s.test_name for s in STRATEGIES]


def get_strategy(number_or_key) -> StrategySpec:
    for s in STRATEGIES:
        if s.number == number_or_key or s.key == number_or_key or s.test_name == number_or_key:
            return s
    raise KeyError(number_or_key)
