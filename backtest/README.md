# calm-paper-trade-2526-backtest

Local Python backtest harness. Every strategy runs over the **same Danish-time windows**
(Europe/Copenhagen, monthly, Jan 2025 - Sep 2026); only the strategy changes.
Standard library only (+ pytest for tests).

## Run

    python scripts/make_sample_data.py --out data/sample_2025_26.csv     # offline SAMPLE data
    python -m calmbt.run --config config/run_config.json --data data/sample_2025_26.csv --out results
    python -m pytest -q

Real data: `python scripts/fetch_data.py ...` (needs network, see data/README.md), then put the new
file's SHA-256 into `dataset_sha256` in `config/run_config.json`. The runner refuses a dataset whose
hash differs from the config. Until then every result is labelled **SAMPLE** (synthetic prices).

## Outputs (`results/`)
- `results.csv` / `results.md`: one row per window per strategy: start/end DKK, PnL DKK after costs,
  return %, fees paid, trades, data label (REAL/SAMPLE), dataset sha256, warnings.
- `run_manifest.json`: full config, UTC conversion of each window, every strategy's settings, and
  `window_end_states` (final position/cash/value per window per strategy).

## Rules of the engine
- Each window: every strategy starts with `starting_capital_dkk` in cash, all-in/all-out (long or cash).
- Signals are decided at a bar's close and filled at the **next bar's open**, with slippage and fees.
- `buy_hold` buys at the open of the first bar in the window.
- Every position is sold at the **close of the last bar that ends at or before the window end**
  (never at the next open). Final value == cash, so results are real cash after costs.
- Windows are converted to UTC per date with zoneinfo (23/25-hour DST days handled, not a fixed offset).
- Prices are in the data's quote currency (USD); `fx_to_dkk` is one constant rate (placeholder).

## Settings (`config/run_config.json`, read-only once loaded)
`fee_bps` is 0 with `fees_unverified=true`: results carry a warning until you enter your broker's
price list. Zero fees flatter the calm strategy more than buy-and-hold (it trades more).
Calm rules live in `calmbt/calm.py` and are PLACEHOLDERS until you paste your pre-2025 rules; do not retune on 2025/26.

Strategies: `buy_hold`, `calm_vol_placeholder`, `calm_user`.
