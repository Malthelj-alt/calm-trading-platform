# data/

Frozen OHLCV datasets for the calm paper-trade backtest. All files are in **Danish time**.

| File | What it is |
|------|------------|
| `<name>.csv` | Bars: `ts_utc,ts_dk,open,high,low,close,volume` |
| `<name>.csv.meta.json` | Provenance: `label` (`REAL`/`SAMPLE`), `source`, `symbol`, `interval`, `rows`, first/last timestamps, `sha256` of the CSV, and more |
| `sample_2025_26.csv` (+ meta) | **SAMPLE**: synthetic, seeded, hourly 2025-01-01 00:00 → 2026-09-30 23:00 DK time. **These are not market prices.** |

## Timestamps
- Each row's timestamp is the **bar open time**.
- `ts_utc` is the authoritative timestamp. `ts_dk` is the same moment in `Europe/Copenhagen`, written with its offset (`+01:00` in winter, `+02:00` in summer). It is converted with Python's `zoneinfo`.
- Daylight-saving changes: **2025-03-30** (02:00 local doesn't exist), **2025-10-26** (02:00–03:00 local happens twice, so the offset is what tells the two apart), and **2026-03-29**. Hourly data is stored on a UTC grid, so it has no gaps or duplicates around these changes.
- Stooq daily bars are stamped at the exchange's session open (US: 09:30 New York, which is 15:30 DK). A daily close only becomes known at the session close.

## Integrity (frozen datasets)
`calmbt.data.load_bars(path)` **refuses to load** a file if any of these is true:
- the `.meta.json` file is missing,
- the CSV's SHA-256 doesn't match `meta.sha256`,
- timestamps aren't sorted or are duplicated, or `ts_dk` doesn't match `ts_utc`,
- the OHLC values are inconsistent, or the row count doesn't match the meta.

`calmbt.data.dataset_fingerprint(path)` returns the SHA-256 that goes into `config/run_config.json`. Don't edit a CSV by hand. To change the data, fetch it again, which creates a new fingerprint.

## Creating datasets
```bash
# offline sample (repeatable: same bytes and same sha256 every time)
python scripts/make_sample_data.py --out data/sample_2025_26.csv

# REAL data (needs network)
python scripts/fetch_data.py --source binance --symbol BTCUSDT --interval 1h \
    --start 2025-01-01 --out data/btcusdt_1h_2025_26.csv
python scripts/fetch_data.py --source stooq --symbol spy.us --interval 1d \
    --start 2025-01-01 --out data/spy_1d_2025_26.csv
```
`--end` defaults to today (UTC, the date itself is excluded). Binance bars that haven't closed yet are dropped.

### Hosts to allowlist for real data
- Binance: `api.binance.com` (fallback `data-api.binance.vision`)
- stooq: `stooq.com`

If these hosts can't be reached, `fetch_data.py` exits with code 2. It writes **nothing** and does **not** fall back to fake data.
**Consequence:** until a REAL dataset exists, every backtest result is on SYNTHETIC SAMPLE data and says nothing about the 2025/26 markets.

## Labels
- `REAL`: downloaded by `fetch_data.py` from a public source.
- `SAMPLE`: synthetic. Every result computed from it must be labelled SAMPLE.
