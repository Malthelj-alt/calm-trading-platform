#!/usr/bin/env python3
"""Generate a deterministic SYNTHETIC hourly dataset for offline checks.

    python scripts/make_sample_data.py --out data/sample_2025_26.csv

* Hourly bars from 2025-01-01 00:00 to 2026-09-30 23:00 Europe/Copenhagen
  (generated on a UTC hourly grid, so the DK clock correctly skips 02:00 on
  2025-03-30 / 2026-03-29 and shows 02:00 twice on 2025-10-26).
* Seeded RNG -> byte-identical CSV and identical SHA-256 on every run.
* meta label is ``SAMPLE``. These are NOT market prices; any result computed
  from this file must be reported as SAMPLE.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from calmbt.data import DK_TZ, UTC, Bar, to_dk, write_dataset  # noqa: E402

START_DK = datetime(2025, 1, 1, 0, 0, tzinfo=DK_TZ)
END_DK = datetime(2026, 9, 30, 23, 0, tzinfo=DK_TZ)  # last bar open (inclusive)


def generate(seed: int, start_price: float = 100.0, hourly_vol: float = 0.004,
             hourly_drift: float = 0.00001) -> list:
    rng = random.Random(seed)
    t = START_DK.astimezone(UTC)
    end = END_DK.astimezone(UTC)
    step = timedelta(hours=1)
    price = start_price
    bars = []
    while t <= end:
        o = price * (1.0 + rng.gauss(0.0, hourly_vol * 0.1))  # small open gap
        c = o * math.exp(hourly_drift + rng.gauss(0.0, hourly_vol))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, hourly_vol * 0.5)))
        l = min(o, c) * (1.0 - abs(rng.gauss(0.0, hourly_vol * 0.5)))
        o, c, h, l = (round(x, 4) for x in (o, c, h, l))
        h = max(h, o, c)
        l = min(l, o, c)
        v = round(rng.uniform(50.0, 500.0), 3)
        bars.append(Bar(t, to_dk(t), o, h, l, c, v))
        price = c
        t += step
    return bars


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/sample_2025_26.csv")
    p.add_argument("--seed", type=int, default=2526)
    p.add_argument("--symbol", default="SAMPLE")
    args = p.parse_args(argv)

    bars = generate(args.seed)
    meta = write_dataset(
        args.out,
        bars,
        {
            "label": "SAMPLE",
            "source": "synthetic (scripts/make_sample_data.py, seeded geometric random walk)",
            "symbol": args.symbol,
            "interval": "1h",
            "quote_currency": "USD",
            "seed": args.seed,
            "warning": "SYNTHETIC DATA - NOT MARKET PRICES. Results from this file are SAMPLE only.",
        },
    )
    print(f"wrote {args.out}  rows={meta['rows']}  label={meta['label']}")
    print(f"  {meta['first_ts_dk']} .. {meta['last_ts_dk']} (Europe/Copenhagen)")
    print(f"  sha256={meta['sha256']}")
    print("  NOTE: SAMPLE data is synthetic; do not report it as 2025/26 market results.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
