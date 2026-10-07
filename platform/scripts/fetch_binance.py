#!/usr/bin/env python3
"""Fetch real BTC/USDT 1h bars from Binance's public klines endpoint.

Standard library only. No API key, no account, read-only market data.
This script never places, signs or sends any order.

Usage:
    python3 scripts/fetch_binance.py                 # -> data/btcusdt_1h.csv (+ .meta.json)
    python3 scripts/fetch_binance.py --bars 920 --out data/btcusdt_1h.csv
    python3 scripts/fetch_binance.py --make-sample   # regenerate the offline sample

CSV format (header row, then one row per closed bar, oldest first):
    open_time_ms,open,high,low,close,volume
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

SYMBOL = "BTCUSDT"
INTERVAL = "1h"
INTERVAL_MS = 3_600_000
DEFAULT_BARS = 920
MAX_LIMIT = 1000  # Binance klines hard cap per request

ENDPOINTS = (
    "https://api.binance.com/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
)

HEADER = ("open_time_ms", "open", "high", "low", "close", "volume")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "data", "btcusdt_1h.csv")
SAMPLE_OUT = os.path.join(ROOT, "data", "sample_btcusdt_1h.csv")

SAMPLE_SEED = 20240101
SAMPLE_START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
SAMPLE_CENTER = Decimal("60000")


class FetchError(RuntimeError):
    pass


def _utc_iso(ms: int | None = None) -> str:
    ts = time.time() if ms is None else ms / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _show(path: str) -> str:
    ap = os.path.abspath(path)
    return os.path.relpath(ap, ROOT) if ap.startswith(ROOT + os.sep) else ap


def _meta_path(csv_path: str) -> str:
    base, _ = os.path.splitext(csv_path)
    return base + ".meta.json"


# --------------------------------------------------------------------------- #
# Validation shared by real and sample data
# --------------------------------------------------------------------------- #
def validate_rows(rows: list[tuple[str, ...]]) -> None:
    if not rows:
        raise FetchError("no bars to write")
    prev_t = None
    for i, r in enumerate(rows):
        t = int(r[0])
        o, h, lo, c, v = (Decimal(x) for x in r[1:])
        if min(o, h, lo, c) <= 0 or v < 0:
            raise FetchError(f"bar {i}: non-positive price or negative volume")
        if h < max(o, c) or lo > min(o, c) or h < lo:
            raise FetchError(f"bar {i}: inconsistent OHLC {r}")
        if prev_t is not None and t - prev_t != INTERVAL_MS:
            raise FetchError(f"bar {i}: gap or disorder in open_time ({prev_t} -> {t})")
        prev_t = t


def write_csv(path: str, rows: list[tuple[str, ...]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(",".join(HEADER) + "\n")
        for r in rows:
            f.write(",".join(r) + "\n")
    os.replace(tmp, path)


def write_meta(path: str, meta: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Real data
# --------------------------------------------------------------------------- #
def _get_json(url: str, timeout: float) -> object:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "calm-paper-trader/1.0 (read-only market data)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise FetchError(f"HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def fetch_klines(bars: int, timeout: float) -> tuple[list[tuple[str, ...]], str]:
    """Return (rows, url_used). Keeps only fully closed bars, last `bars` of them."""
    if not 1 <= bars < MAX_LIMIT:
        raise FetchError(f"--bars must be between 1 and {MAX_LIMIT - 1}")
    # One extra so we can drop the still-forming current bar.
    query = urllib.parse.urlencode({"symbol": SYMBOL, "interval": INTERVAL, "limit": bars + 1})
    errors = []
    for base in ENDPOINTS:
        url = f"{base}?{query}"
        try:
            data = _get_json(url, timeout)
        except (urllib.error.URLError, OSError, ValueError, FetchError) as exc:
            errors.append(f"{base}: {exc}")
            continue
        if not isinstance(data, list):
            errors.append(f"{base}: unexpected response {str(data)[:120]}")
            continue
        now_ms = int(time.time() * 1000)
        rows = []
        for k in data:
            # [open_time, open, high, low, close, volume, close_time, ...]
            if int(k[6]) >= now_ms:
                continue  # bar not closed yet
            rows.append((str(int(k[0])), str(k[1]), str(k[2]), str(k[3]), str(k[4]), str(k[5])))
        rows = rows[-bars:]
        if len(rows) < bars:
            errors.append(f"{base}: only {len(rows)} closed bars returned, wanted {bars}")
            continue
        return rows, url
    raise FetchError("could not reach Binance public market data:\n  " + "\n  ".join(errors))


def run_fetch(bars: int, out: str, timeout: float) -> int:
    try:
        rows, url = fetch_klines(bars, timeout)
        validate_rows(rows)
    except FetchError as exc:
        print(f"fetch_binance: ERROR: {exc}", file=sys.stderr)
        print(
            "fetch_binance: network unavailable or blocked. Nothing was written.\n"
            "  Offline alternative: use data/sample_btcusdt_1h.csv (sample, not real market).",
            file=sys.stderr,
        )
        return 2
    write_csv(out, rows)
    meta = {
        "source": "binance-public",
        "source_url": url,
        "fetched_at_utc": _utc_iso(),
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "bars": len(rows),
        "first_open_time_ms": int(rows[0][0]),
        "last_open_time_ms": int(rows[-1][0]),
        "first_open_time_utc": _utc_iso(int(rows[0][0])),
        "last_open_time_utc": _utc_iso(int(rows[-1][0])),
        "columns": list(HEADER),
        "notes": "Public read-only klines, closed bars only. No key used. Real market data.",
    }
    write_meta(_meta_path(out), meta)
    print(f"fetch_binance: wrote {len(rows)} bars to {_show(out)} "
          f"({meta['first_open_time_utc']} .. {meta['last_open_time_utc']})")
    return 0


# --------------------------------------------------------------------------- #
# Deterministic offline sample
# --------------------------------------------------------------------------- #
def _q(x: float, places: str) -> str:
    return str(Decimal(repr(x)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def generate_sample(bars: int = DEFAULT_BARS, seed: int = SAMPLE_SEED) -> list[tuple[str, ...]]:
    """Seeded, mean-reverting random walk around 60000 with consistent OHLC."""
    rng = random.Random(seed)
    center = float(SAMPLE_CENTER)
    close = center
    rows = []
    for i in range(bars):
        t = SAMPLE_START_MS + i * INTERVAL_MS
        o = float(_q(close, "0.01"))
        # small drift back toward the centre + noise (~0.6% hourly sigma)
        ret = 0.02 * (center - o) / center + rng.gauss(0.0, 0.006)
        c = float(_q(o * (1.0 + ret), "0.01"))
        body_hi, body_lo = max(o, c), min(o, c)
        h = body_hi * (1.0 + abs(rng.gauss(0.0, 0.0025)))
        lo = body_lo * (1.0 - abs(rng.gauss(0.0, 0.0025)))
        h_s, lo_s = _q(h, "0.01"), _q(lo, "0.01")
        # rounding guard: keep high >= max(o,c) and low <= min(o,c)
        if Decimal(h_s) < Decimal(_q(body_hi, "0.01")):
            h_s = _q(body_hi, "0.01")
        if Decimal(lo_s) > Decimal(_q(body_lo, "0.01")):
            lo_s = _q(body_lo, "0.01")
        vol = 400.0 + abs(rng.gauss(0.0, 1.0)) * 600.0 + abs(ret) * 60000.0
        rows.append((str(t), _q(o, "0.01"), h_s, lo_s, _q(c, "0.01"), _q(vol, "0.001")))
        close = c
    return rows


def run_make_sample(out: str) -> int:
    rows = generate_sample()
    validate_rows(rows)
    write_csv(out, rows)
    meta = {
        "source": "sample (not real market)",
        "source_url": None,
        "generator": "scripts/fetch_binance.py --make-sample",
        "seed": SAMPLE_SEED,
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "bars": len(rows),
        "first_open_time_ms": int(rows[0][0]),
        "last_open_time_ms": int(rows[-1][0]),
        "first_open_time_utc": _utc_iso(int(rows[0][0])),
        "last_open_time_utc": _utc_iso(int(rows[-1][0])),
        "columns": list(HEADER),
        "notes": "SAMPLE DATA - deterministic seeded random walk around 60000. "
                 "Not real market prices. For offline runs and tests only.",
    }
    write_meta(_meta_path(out), meta)
    print(f"fetch_binance: wrote {len(rows)} sample bars to {_show(out)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fetch BTCUSDT 1h klines from Binance public API (no key).")
    p.add_argument("--bars", type=int, default=DEFAULT_BARS, help="closed bars to keep (default 920)")
    p.add_argument("--out", default=None, help="output CSV path")
    p.add_argument("--timeout", type=float, default=15.0, help="per-request timeout in seconds")
    p.add_argument("--make-sample", action="store_true",
                   help="regenerate the deterministic offline sample instead of fetching")
    a = p.parse_args(argv)
    if a.make_sample:
        return run_make_sample(a.out or SAMPLE_OUT)
    return run_fetch(a.bars, a.out or DEFAULT_OUT, a.timeout)


if __name__ == "__main__":
    sys.exit(main())
