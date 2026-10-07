# Calm Trading Platform

Paper trading and backtesting for calm, rule-based strategies. Built by CORE REACTOR's agent team.
**Simulated only:** there is no code path that sends a real order, and no accounts or API keys are needed.

| Folder | What it is | Python |
|---|---|---|
| [`platform/`](platform/) | The paper trader: five agents (Data → Strategy → Risk → Paper Execution → Journal) run 10 strategies side by side on the same BTC/USDT bars, with an append-only SQLite ledger and a calm web dashboard. | 3.9+, standard library only |
| [`backtest/`](backtest/) | The 2025/26 backtest: every strategy runs over the same Danish-time (Europe/Copenhagen) monthly windows; only the strategy changes. Buy-and-hold sells at the end of each window, so every result is cash after costs. | 3.11+, plus pytest |

## Platform

```bash
cd platform
python3 scripts/fetch_binance.py                 # optional: real BTC/USDT 1h bars (public API, no key)
python3 -m tradebot run --data data/sample_btcusdt_1h.csv --db trader.db   # or data/btcusdt_1h.csv
python3 -m tradebot serve --db trader.db --port 8000                        # http://localhost:8000
python3 -m unittest                              # 32 tests
```

Every test uses the same bars, clock, $50 start, 0.10% fee and 0.05% slippage per side, a 200-bar warm-up and
fills at the next bar's open. Each run is repeated at a 0.40% fee as a stress case. Release-gate tests check
that a replay gives an identical ledger hash, that future bars can't change past decisions, t+1 fills, and
buy-and-hold against a hand formula.

## Backtest (2025/26, Danish time)

```bash
cd backtest
python -m calmbt.run --config config/run_config.json --data data/sample_2025_26.csv --out results
python -m pytest -q                              # 61 tests
```

Before trusting any number:
1. Fetch real data: `python scripts/fetch_data.py --source binance --symbol BTCUSDT --interval 1h --start 2025-01-01 --end 2026-10-06 --out data/real_2025_26.csv`, and put its SHA-256 (in the `.meta.json`) into `dataset_sha256` in `config/run_config.json`.
2. Paste your original calm rules and pre-2025 settings into `calmbt/calm.py` (it holds labelled placeholders). Don't tune them on 2025/26 data.
3. Enter your broker's real fees in `config/run_config.json` and set `fees_unverified` to `false`.

Results made from the bundled sample data are synthetic and labelled **SAMPLE**. See `backtest/README.md` and `backtest/docs/STRATEGY_REVIEW.md`.

## Origin

`platform/` comes from CORE REACTOR run EXEC-2026-10-06-00003 and `backtest/` from EXEC-2026-10-06-00005.
