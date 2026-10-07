"""Calm strategies for the 2025/26 out-of-sample backtest.

Owner: P4 (quant strategy research).

CONTRACT (read by calmbt/strategies.py and calmbt/engine.py)
------------------------------------------------------------
Every strategy class here provides:

    name: str                    registry key, also written to the manifest
    is_placeholder: bool         True while the rules are not the user's own
    execution_lag_bars: int = 1  a decision on bar t is filled at the OPEN of bar t+1
    params: Mapping              read-only view of the frozen settings it uses
    reset() -> None              called by the engine at the start of every window
    on_bar(bar, position, history) -> int   target position, 0 (cash) or 1 (long)

Semantics of on_bar:
  * ``bar`` is the bar that has just CLOSED (bar t). Its open/high/low/close
    are all known at that moment.
  * ``history`` is the bars that closed before ``bar`` (oldest first). For
    convenience it may also end with ``bar`` itself; that is detected and not
    counted twice. It must never contain a bar later than ``bar``; if it
    does, LookaheadError is raised instead of returning a silent result.
  * ``position`` is the position currently held (0 or 1), i.e. after any fill
    at the open of bar t.
  * The returned target is acted on at the OPEN of bar t+1 (never at bar t's
    close and never at bar t's open). If bar t is the last bar at or before
    the window end, the engine ignores the target and sells everything at
    bar t's close (forced window-end liquidation, see docs/STRATEGY_REVIEW.md).

Settings are frozen in CALM_PARAMS_PRE2025. They must equal the values that
were in use BEFORE 2025-01-01. Do not tune them on 2025/26 data.

Python 3.9+ compatible, standard library only.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CALM_PARAMS_PRE2025",
    "PARAMS_FROZEN_AS_OF",
    "LookaheadError",
    "CalmStrategy",
    "CalmVolPlaceholder",
    "CalmUserRules",
    "CALM_STRATEGIES",
    "make_calm_strategy",
    "simulate_targets",
    "realised_vol",
]


# ---------------------------------------------------------------------------
# Frozen settings
# ---------------------------------------------------------------------------

#: Date the settings below were last allowed to change. Anything later is
#: in-sample contamination for a 2025/26 test.
PARAMS_FROZEN_AS_OF = "2024-12-31"


def _freeze(obj: Any) -> Any:
    """Recursively turn dicts into read-only mappings and lists into tuples."""
    if isinstance(obj, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_freeze(v) for v in obj)
    return obj


def thaw(obj: Any) -> Any:
    """Plain-dict/list copy of a frozen object (for json.dump in the manifest)."""
    if isinstance(obj, Mapping):
        return {k: thaw(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [thaw(v) for v in obj]
    return obj


CALM_PARAMS_PRE2025: Mapping[str, Any] = _freeze(
    {
        # ------------------------------------------------------------------
        # PLACEHOLDER settings. These are NOT the user's original calm
        # paper-trade settings (those files could not be reached). They are
        # reasonable, untuned defaults so the harness runs end to end.
        # ------------------------------------------------------------------
        "calm_vol_placeholder": {
            "is_placeholder": True,
            # Number of past log returns in the rolling realised-vol estimate.
            # With hourly bars, 24 = one day of trading for crypto.
            "vol_lookback_bars": 24,
            # Hold only while per-bar realised vol (stdev of log returns, NOT
            # annualised, so it does not depend on a bars-per-year guess) is
            # strictly below this value. 0.006 = 0.6 % per hourly bar.
            "vol_threshold_per_bar": 0.006,
            # Until this many returns are available the strategy stays in
            # cash. Warm-up uses only bars inside the window the engine
            # passes, so no data from outside the window leaks in.
            "min_returns_required": 24,
        },
        # ------------------------------------------------------------------
        # >>> USER: PASTE YOUR ORIGINAL PRE-2025 CALM SETTINGS HERE <<<
        # Copy the values exactly as they were in your calm paper trade
        # before 2025-01-01. Set "is_placeholder" to False once done.
        # Do NOT change them after looking at any 2025/26 result.
        # ------------------------------------------------------------------
        "calm_user": {
            "is_placeholder": True,
            # Until you paste your rules, calm_user runs the placeholder rule
            # with these values so the pipeline stays runnable.
            "vol_lookback_bars": 24,
            "vol_threshold_per_bar": 0.006,
            "min_returns_required": 24,
        },
        # >>> END OF USER SETTINGS <<<
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class LookaheadError(RuntimeError):
    """Raised when a strategy is handed a bar from the future."""


def _get(bar: Any, field: str) -> Any:
    if isinstance(bar, Mapping):
        return bar[field]
    return getattr(bar, field)


def _ts(bar: Any) -> Any:
    """Best available ordering key for a bar (ts_utc preferred)."""
    for field in ("ts_utc", "ts", "timestamp", "ts_dk"):
        try:
            return _get(bar, field)
        except (KeyError, AttributeError):
            continue
    return None


def _past_bars(bar: Any, history: Optional[Sequence[Any]]) -> List[Any]:
    """Return bars up to and including ``bar``; raise on any future bar."""
    hist = list(history or ())
    t_now = _ts(bar)
    if hist:
        last = hist[-1]
        same = last is bar or (t_now is not None and _ts(last) == t_now)
        if same:
            hist = hist[:-1]
    if t_now is not None:
        for h in hist:
            th = _ts(h)
            if th is not None and th >= t_now:
                raise LookaheadError(
                    f"history contains bar at {th!r}, not before current bar {t_now!r}"
                )
    hist.append(bar)
    return hist


def realised_vol(closes: Sequence[float], lookback: int) -> Optional[float]:
    """Sample stdev of the last ``lookback`` log returns of ``closes``.

    Uses only the closes given (the caller passes closes up to bar t).
    Returns None when fewer than ``lookback`` returns exist or a price <= 0.
    """
    if lookback < 2 or len(closes) < lookback + 1:
        return None
    window = closes[-(lookback + 1):]
    rets = []
    for prev, cur in zip(window[:-1], window[1:]):
        if prev <= 0 or cur <= 0:
            return None
        rets.append(math.log(cur / prev))
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


# ---------------------------------------------------------------------------
# Strategy base
# ---------------------------------------------------------------------------

class CalmStrategy:
    """Common interface. Subclasses implement ``decide``."""

    name: str = "calm_base"
    params_key: str = ""
    execution_lag_bars: int = 1  # fixed: fill at the open of bar t+1

    def __init__(self, params: Optional[Mapping[str, Any]] = None) -> None:
        if params is None:
            params = CALM_PARAMS_PRE2025[self.params_key]
        self.params: Mapping[str, Any] = _freeze(dict(params))
        self.is_placeholder: bool = bool(self.params.get("is_placeholder", True))
        self.reset()

    # Engine calls this at the start of every window so no state crosses
    # window boundaries (each window is an independent cash-to-cash trade).
    def reset(self) -> None:
        self._closes: List[float] = []

    def describe(self) -> Dict[str, Any]:
        """JSON-friendly description for the run manifest."""
        return {
            "name": self.name,
            "class": type(self).__name__,
            "is_placeholder": self.is_placeholder,
            "execution_lag_bars": self.execution_lag_bars,
            "params_frozen_as_of": PARAMS_FROZEN_AS_OF,
            "params": thaw(self.params),
        }

    def on_bar(self, bar: Any, position: int, history: Optional[Sequence[Any]] = None) -> int:
        bars = _past_bars(bar, history)
        target = self.decide(bars, int(position))
        if target not in (0, 1):
            raise ValueError(f"{self.name}: target must be 0 or 1, got {target!r}")
        return int(target)

    def decide(self, bars: List[Any], position: int) -> int:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# PLACEHOLDER calm strategy (not the user's rules)
# ---------------------------------------------------------------------------

class CalmVolPlaceholder(CalmStrategy):
    """PLACEHOLDER: long only while rolling realised volatility is calm.

    Rule at the close of bar t:
        vol_t = stdev(log(C_i / C_{i-1}) for the last N returns ending at t)
        target = 1 if vol_t < threshold else 0
        target = 0 while fewer than ``min_returns_required`` returns exist.
    Only closes up to and including bar t are used. The target is filled at
    the open of bar t+1 by the engine.
    """

    name = "calm_vol_placeholder"
    params_key = "calm_vol_placeholder"

    def decide(self, bars: List[Any], position: int) -> int:
        p = self.params
        n = int(p["vol_lookback_bars"])
        need = max(n, int(p.get("min_returns_required", n)))
        closes = [float(_get(b, "close")) for b in bars]
        if len(closes) - 1 < need:
            return 0
        vol = realised_vol(closes, n)
        if vol is None:
            return 0
        return 1 if vol < float(p["vol_threshold_per_bar"]) else 0


# ---------------------------------------------------------------------------
# >>> USER: PASTE YOUR ORIGINAL PRE-2025 CALM RULES HERE <<<
# ---------------------------------------------------------------------------
# 1. Replace the body of CalmUserRules.decide with your original rule.
#    ``bars`` holds every bar of the current window up to and including the
#    bar that just closed (bars[-1]). Use only bars[...] - nothing else.
#    Read OHLC with _get(bar, "close") etc. so dicts and dataclasses work.
# 2. Put every number your rule uses in CALM_PARAMS_PRE2025["calm_user"]
#    and read it from self.params. No numbers hard-coded here.
# 3. Set "is_placeholder": False in CALM_PARAMS_PRE2025["calm_user"].
# 4. Return 1 to be long from the next bar's open, 0 to be in cash.
# Until you do this, calm_user simply runs the placeholder volatility rule
# and every report row carries is_placeholder = True.
# ---------------------------------------------------------------------------

class CalmUserRules(CalmStrategy):
    """Slot for the user's original calm paper-trade rules (pre-2025)."""

    name = "calm_user"
    params_key = "calm_user"

    def decide(self, bars: List[Any], position: int) -> int:
        # ---- BEGIN USER RULES (replace this block) --------------------------
        return CalmVolPlaceholder.decide(self, bars, position)  # type: ignore[arg-type]
        # ---- END USER RULES -------------------------------------------------

# >>> END OF USER RULES <<<


# ---------------------------------------------------------------------------
# Registry + reference simulation
# ---------------------------------------------------------------------------

CALM_STRATEGIES: Mapping[str, type] = MappingProxyType(
    {
        CalmVolPlaceholder.name: CalmVolPlaceholder,
        CalmUserRules.name: CalmUserRules,
    }
)


def make_calm_strategy(name: str) -> CalmStrategy:
    try:
        cls = CALM_STRATEGIES[name]
    except KeyError:
        raise KeyError(f"unknown calm strategy {name!r}; known: {sorted(CALM_STRATEGIES)}")
    return cls()


def simulate_targets(strategy: CalmStrategy, bars: Iterable[Any]) -> List[Tuple[int, int]]:
    """Reference timing model for one window (no prices/fees).

    Returns, per bar, ``(position_held_during_bar, target_decided_at_close)``.
    The position held during bar t+1 equals the target decided at bar t, i.e.
    fills happen at the open of t+1. The final element's position after the
    window is forced to 0 by the engine (window-end liquidation at the close
    of the last bar). Useful for tests and for checking the engine's timing.
    """
    strategy.reset()
    seen: List[Any] = []
    out: List[Tuple[int, int]] = []
    pos = 0
    for bar in bars:
        target = strategy.on_bar(bar, pos, seen)
        out.append((pos, target))
        seen.append(bar)
        pos = target  # becomes effective at the next bar's open
    return out
