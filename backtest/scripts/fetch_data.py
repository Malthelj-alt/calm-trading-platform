#!/usr/bin/env python3
"""Download REAL 2025/26 bars and freeze them as CSV + SHA-256 meta (Danish time).

Examples
--------
    # crypto, hourly, Binance public klines (no API key)
    python scripts/fetch_data.py --source binance --symbol BTCUSDT --interval 1h \\
        --start 2025-01-01 --out data/btcusdt_1h_2025_26.csv

    # stock / ETF, daily, stooq CSV (no API key)
    python scripts/fetch_data.py --source stooq --symbol spy.us --interval 1d \\
        --start 2025-01-01 --out data/spy_1d_2025_26.csv

Hosts that must be reachable (allowlist these to unblock the real-data path):
    binance : api.binance.com  (fallback: data-api.binance.vision)
    stooq   : stooq.com

Behaviour
---------
* Timestamps are bar OPEN times, stored in UTC and converted to
  Europe/Copenhagen with zoneinfo (DST 2025-03-30, 2025-10-26, 2026-03-29).
* Bars are sorted and de-duplicated; Binance bars that have not closed yet
  are dropped.
* Writes ``<out>`` and ``<out>.meta.json`` with label ``REAL``.
* If the network is unavailable or the source returns no usable data the
  script exits non-zero with a clear message and writes NOTHING.
  It never substitutes synthetic data.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from calmbt.data import UTC, Bar, format_ts, make_bar, utc_from_ms, write_dataset  # noqa: E402

EXIT_NETWORK = 2
EXIT_NO_DATA = 3
EXIT_BAD_ARGS = 4

USER_AGENT = "calmbt-fetch/0.1 (+local backtest; stdlib urllib)"
TIMEOUT_S = 30

BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
STOOQ_HOST = "https://stooq.com"
ALLOWLIST = {"binance": ["api.binance.com", "data-api.binance.vision"], "stooq": ["stooq.com"]}

BINANCE_INTERVALS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200,
    "1d": 86400,
}
STOOQ_INTERVALS = {"1d": "d", "1w": "w", "1M": "m"}

# Exchange-local session open used to timestamp stooq daily bars (bar open time).
STOOQ_SESSION_OPEN = {
    "us": ("America/New_York", 9, 30),
    "de": ("Europe/Berlin", 9, 0),
    "uk": ("Europe/London", 8, 0),
    "jp": ("Asia/Tokyo", 9, 0),
    "hk": ("Asia/Hong_Kong", 9, 30),
}
STOOQ_DEFAULT_SESSION = ("Europe/Copenhagen", 9, 0)  # Nasdaq Copenhagen


class FetchError(Exception):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace") if exc.fp else ""
        raise FetchError(f"HTTP {exc.code} from {url}: {body}", EXIT_NO_DATA) from exc
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        raise FetchError(f"network error contacting {url}: {exc}", EXIT_NETWORK) from exc


# --------------------------------------------------------------------------- #
# Binance
# --------------------------------------------------------------------------- #
def fetch_binance(symbol: str, interval: str, start: datetime, end: datetime) -> Tuple[List[Bar], str, int]:
    if interval not in BINANCE_INTERVALS:
        raise FetchError(f"unsupported binance interval {interval!r}; use one of {sorted(BINANCE_INTERVALS)}", EXIT_BAD_ARGS)
    step_ms = BINANCE_INTERVALS[interval] * 1000
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000) - 1
    now_ms = int(time.time() * 1000)

    last_err: Optional[FetchError] = None
    for host in BINANCE_HOSTS:
        try:
            bars: List[Bar] = []
            cursor = start_ms
            while cursor <= end_ms:
                q = urllib.parse.urlencode(
                    {"symbol": symbol.upper(), "interval": interval,
                     "startTime": cursor, "endTime": end_ms, "limit": 1000}
                )
                rows = json.loads(http_get(f"{host}/api/v3/klines?{q}").decode("utf-8"))
                if not isinstance(rows, list):
                    raise FetchError(f"unexpected binance response: {str(rows)[:200]}", EXIT_NO_DATA)
                if not rows:
                    break
                for r in rows:
                    open_ms, o, h, l, c, v, close_ms = int(r[0]), r[1], r[2], r[3], r[4], r[5], int(r[6])
                    if close_ms >= now_ms:  # bar still forming -> not final, drop
                        continue
                    bars.append(make_bar(utc_from_ms(open_ms), float(o), float(h), float(l), float(c), float(v)))
                nxt = int(rows[-1][0]) + step_ms
                if nxt <= cursor:
                    break
                cursor = nxt
                time.sleep(0.1)  # be polite to the public endpoint
            return bars, host, step_ms
        except FetchError as exc:
            last_err = exc
            if exc.code != EXIT_NETWORK:
                raise
            print(f"[fetch] {host} unreachable, trying next host ...", file=sys.stderr)
    assert last_err is not None
    raise last_err


def count_gaps(bars: List[Bar], step_ms: int) -> int:
    missing = 0
    for a, b in zip(bars, bars[1:]):
        d = int((b.ts_utc - a.ts_utc).total_seconds() * 1000)
        if d > step_ms:
            missing += d // step_ms - 1
    return missing


# --------------------------------------------------------------------------- #
# stooq
# --------------------------------------------------------------------------- #
def stooq_session(symbol: str) -> Tuple[str, int, int]:
    suffix = symbol.lower().rsplit(".", 1)[-1] if "." in symbol else ""
    return STOOQ_SESSION_OPEN.get(suffix, STOOQ_DEFAULT_SESSION)


def fetch_stooq(symbol: str, interval: str, start: datetime, end: datetime) -> Tuple[List[Bar], str, dict]:
    if interval not in STOOQ_INTERVALS:
        raise FetchError(f"unsupported stooq interval {interval!r}; use one of {sorted(STOOQ_INTERVALS)}", EXIT_BAD_ARGS)
    q = urllib.parse.urlencode(
        {"s": symbol.lower(), "i": STOOQ_INTERVALS[interval],
         "d1": start.strftime("%Y%m%d"), "d2": (end - timedelta(days=1)).strftime("%Y%m%d")}
    )
    url = f"{STOOQ_HOST}/q/d/l/?{q}"
    text = http_get(url).decode("utf-8", "replace")
    if not text.lstrip().lower().startswith("date,"):
        raise FetchError(
            f"stooq did not return CSV for {symbol!r} (first bytes: {text[:120]!r}). "
            "Check the symbol (e.g. spy.us, novo-b.dk?) or whether stooq now requires a key/captcha.",
            EXIT_NO_DATA,
        )
    tz_name, hh, mm = stooq_session(symbol)
    tz = ZoneInfo(tz_name)
    bars: List[Bar] = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            d = date.fromisoformat(row["Date"])
            o, h, l, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
            v = float(row.get("Volume") or 0.0)
        except (KeyError, ValueError):
            continue
        local_open = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
        if not (start <= local_open.astimezone(UTC) < end):
            continue
        bars.append(make_bar(local_open, o, h, l, c, v))
    info = {
        "session_open_timezone": tz_name,
        "session_open_local": f"{hh:02d}:{mm:02d}",
        "bar_caveat": "daily bar stamped at session open; its close is only known at session close, "
                      "so windows must end at/after the session close for the close to be usable.",
    }
    return bars, url, info


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True, choices=["binance", "stooq"])
    p.add_argument("--symbol", required=True, help="e.g. BTCUSDT (binance) or spy.us (stooq)")
    p.add_argument("--interval", default="1h", help="binance: 1m..1d; stooq: 1d|1w|1M")
    p.add_argument("--start", default="2025-01-01", help="UTC date, inclusive (default 2025-01-01)")
    p.add_argument("--end", default=date.today().isoformat(), help="UTC date, exclusive (default today)")
    p.add_argument("--out", required=True, help="output CSV path, e.g. data/btcusdt_1h_2025_26.csv")
    args = p.parse_args(argv)

    try:
        start, end = parse_day(args.start), parse_day(args.end)
    except ValueError as exc:
        print(f"ERROR: bad date: {exc}", file=sys.stderr)
        return EXIT_BAD_ARGS
    if end <= start:
        print("ERROR: --end must be after --start", file=sys.stderr)
        return EXIT_BAD_ARGS

    extra: dict = {}
    try:
        if args.source == "binance":
            bars, host, step_ms = fetch_binance(args.symbol, args.interval, start, end)
            source_url = f"{host}/api/v3/klines"
            quote = "USDT" if args.symbol.upper().endswith("USDT") else "see symbol"
        else:
            bars, source_url, extra = fetch_stooq(args.symbol, args.interval, start, end)
            step_ms = 0
            quote = "USD" if args.symbol.lower().endswith(".us") else "see symbol"
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if exc.code == EXIT_NETWORK:
            hosts = ", ".join(ALLOWLIST[args.source])
            print(
                "\n*** HIGH SEVERITY: REAL 2025/26 DATA COULD NOT BE DOWNLOADED. ***\n"
                "No dataset was written and no synthetic substitute was created.\n"
                "Consequence: until this succeeds, every backtest result in this project is\n"
                "computed on SYNTHETIC SAMPLE data and says nothing about 2025/26 markets.\n"
                f"To fix: allow outbound HTTPS to: {hosts}  then re-run this command.",
                file=sys.stderr,
            )
        return exc.code

    if not bars:
        print(f"ERROR: source returned 0 bars for {args.symbol} {args.start}..{args.end}; nothing written.",
              file=sys.stderr)
        return EXIT_NO_DATA

    meta = {
        "label": "REAL",
        "source": args.source,
        "source_url": source_url,
        "symbol": args.symbol,
        "interval": args.interval,
        "quote_currency": quote,
        "requested_start_utc": format_ts(start),
        "requested_end_utc_exclusive": format_ts(end),
        "fetched_at_utc": format_ts(datetime.now(UTC)),
        "fetch_script": "scripts/fetch_data.py",
    }
    meta.update(extra)
    if step_ms:
        sb = sorted({b.ts_utc: b for b in bars}.values(), key=lambda b: b.ts_utc)
        meta["missing_bars_in_range"] = count_gaps(sb, step_ms)

    full = write_dataset(args.out, bars, meta)
    print(f"wrote {args.out}  rows={full['rows']}  label=REAL")
    print(f"  {full['first_ts_dk']} .. {full['last_ts_dk']} (Europe/Copenhagen)")
    print(f"  sha256={full['sha256']}")
    if full.get("missing_bars_in_range"):
        print(f"  WARNING: {full['missing_bars_in_range']} missing bars (exchange gaps) — see meta.")
    print("  Put this sha256 into config/run_config.json (dataset fingerprint) to lock the run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
