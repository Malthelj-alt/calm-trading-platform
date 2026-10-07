"""Shared offline fixtures: small fake bars and configs built inside the tests."""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from calmbt import data as dmod  # noqa: E402
from calmbt.config import _freeze, windows_utc  # noqa: E402

UTC = timezone.utc
HOUR = timedelta(hours=1)


def utc(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def make_cfg(windows: List[Dict[str, str]], **over: Any) -> Dict[str, Any]:
    """Plain-dict config with every required key. Zero fees, zero slippage by default."""
    cfg: Dict[str, Any] = {
        "timezone": "Europe/Copenhagen",
        "windows": windows,
        "starting_capital_dkk": 100000.0,
        "fx_to_dkk": 1.0,
        "fx_source_note": "test constant",
        "fee_bps": 0.0,
        "fees_unverified": True,
        "slippage_bps": 0.0,
        "bar_interval": "1h",
        "dataset_sha256": "0" * 64,
        "force_flat_at_window_end": True,
        "strategies": ["buy_hold", "calm_vol_placeholder", "calm_user"],
    }
    cfg.update(over)
    return cfg


def frozen(cfg: Dict[str, Any]):
    return _freeze(cfg)


def window(cfg: Dict[str, Any], idx: int = 0):
    return windows_utc(frozen(cfg))[idx]


def hourly_bars(start: datetime, n: int,
                px: Optional[Callable[[int], float]] = None,
                step: timedelta = HOUR) -> List[dmod.Bar]:
    """n bars opening at start, start+step, ... open != close so fills are distinguishable."""
    px = px or (lambda i: 100.0 + i)
    out = []
    for i in range(n):
        base = px(i)
        o, c = base, base + 0.5
        out.append(dmod.make_bar(start + i * step, o, max(o, c) + 0.25, min(o, c) - 0.25, c, 10.0))
    return out


def wiggly_price(i: int) -> float:
    """Deterministic price: quiet stretches then bursts, so the calm rules both enter and exit."""
    quiet = 100.0 + 0.02 * math.sin(i / 3.0)
    if (i // 40) % 2 == 1:
        return quiet + 4.0 * math.sin(i * 1.7) * (1 + (i % 5))
    return quiet


@pytest.fixture
def one_month_windows():
    return [{"name": "w1", "start": "2025-01-10T00:00:00", "end": "2025-01-14T00:00:00"},
            {"name": "w2", "start": "2025-07-10T00:00:00", "end": "2025-07-14T00:00:00"}]


@pytest.fixture
def frozen_dataset(tmp_path):
    """A small frozen CSV + meta (SAMPLE label) covering winter and summer windows."""
    bars = (hourly_bars(utc(2025, 1, 9, 12), 24 * 6, wiggly_price)
            + hourly_bars(utc(2025, 7, 9, 12), 24 * 6, wiggly_price))
    path = str(tmp_path / "tiny.csv")
    meta = dmod.write_dataset(path, bars, {"label": "SAMPLE", "source": "test", "symbol": "TEST",
                                           "interval": "1h"})
    return path, meta, bars


@pytest.fixture
def cli_config(tmp_path, frozen_dataset, one_month_windows):
    path, meta, _ = frozen_dataset
    cfg = make_cfg(one_month_windows, dataset_sha256=meta["sha256"], fee_bps=10.0, slippage_bps=5.0)
    cp = tmp_path / "cfg.json"
    cp.write_text(json.dumps(cfg))
    return str(cp), path, cfg
