# Strategy validity review: calm paper trade, 2025/26 out-of-sample test

Author: Ada Lindgren (quant strategy research, package P4)
Scope: `calmbt/calm.py` plus how the engine (`calmbt/engine.py`), settings (`config/run_config.json`) and data (`calmbt/data.py`) need to behave for the 2025/26 numbers to mean anything.

## 0. Summary

| Item | Status |
|---|---|
| Calm rules | **PLACEHOLDER.** We could not reach the original calm paper-trade files. `calm_vol_placeholder` and `calm_user` both run a simple low-volatility rule until you paste your own rules. Every result row carries `is_placeholder = true`. |
| Settings | Kept in `CALM_PARAMS_PRE2025`, which is read-only and dated `PARAMS_FROZEN_AS_OF = 2024-12-31`. |
| Execution timing | A decision made on bar *t* is filled at the **open of bar t+1**. This is fixed (`execution_lag_bars = 1`). |
| Window end | **Every** strategy, including buy-and-hold, is sold at the close of the last bar at or before the window end. Results compare cash with cash. |
| Fees | **UNVERIFIED.** `fee_bps = 0` and `fees_unverified = true` until you enter your broker's price list. All results overstate net returns until then. |
| Data | Sample data is fake and labelled `SAMPLE`. Real numbers exist only after `scripts/fetch_data.py` has run and its SHA-256 is locked into the settings. |

**Bottom line:** until the real rules, the real price list and real data are in place, this harness checks the plumbing only. No conclusion about the calm strategy should be drawn from a `SAMPLE` or `is_placeholder` row.

---

## 1. Look-ahead bias

Look-ahead bias means using information at a time when it was not yet available. In bar-based backtests it is the most common reason results look too good. These are the specific risks and the guard against each:

| Risk | Guard |
|---|---|
| Deciding on bar *t*'s close and filling at that same close (or worse, at bar *t*'s open). | The contract in `calm.py`: `on_bar` sees bar *t* only after it has closed, and its target is filled at the **open of t+1**. `simulate_targets()` is the reference timing model. The engine must use the same lag. |
| The strategy being handed bars from the future. | `on_bar` checks that every bar in `history` has a timestamp strictly earlier than the current bar. If not, it raises `LookaheadError` instead of quietly returning a value. |
| Indicators computed over the whole series (for example a full-sample mean or z-score) and then sliced. | Realised volatility is computed from closes up to and including bar *t* only (`realised_vol`). A smoke test changed every bar after *t* and confirmed that decisions up to *t* stay the same. |
| Settings chosen with knowledge of 2025/26 (see section 3). | The settings are frozen and dated. They are not tuned on the test period. |
| Window-end exit at the next bar's open. After a weekend or holiday gap, that open lies **after** the window end. | Forced exit at the **close of the last bar at or before** the window end (engine contract; tested in `tests/test_buy_hold.py`). |
| Timezone shifts. A window defined in Danish time but cut with a fixed +01:00 offset is wrong by one hour for half the year, so bars are moved into or out of a window. | Windows are converted with `zoneinfo` `Europe/Copenhagen` for each date. The changes on 2025-03-30, 2025-10-26 and 2026-03-29 are tested in `tests/test_dst_windows.py`. |
| Daily stock bars stamped at midnight but only known at the 17:30 CET close (stooq). | Treat the close of a daily bar as known only after the market close. Do not mix daily stock bars with hourly crypto bars in one decision. For daily bars, filling at the next open is the earliest honest fill. |
| Warm-up taken from before the window start. | Not look-ahead, but it makes windows depend on each other. The engine calls `reset()` at each window start. The placeholder stays in cash until it has `min_returns_required` returns **inside** the window. This costs some exposure early in each window, and the cost is identical across all runs. |

Remaining look-ahead risk: if your pasted rules read anything other than `bars` (a global series, a file, a pre-computed column), the guard cannot see it. Keep the rule pure: use only the `bars` argument.

## 2. Overfitting

* **The test is only out-of-sample if nothing was chosen after looking at 2025/26.** That covers settings, the choice of rule variant, the choice of symbol, the choice of windows, and even the choice to drop a strategy that "did badly". Each choice made after seeing results is one more fitted degree of freedom.
* **Multiple comparisons.** With roughly 21 monthly windows and several strategies, at least one strategy will beat buy-and-hold in some windows by chance. Report the full table, not the best rows. Look at the distribution across windows (median, share of windows won, worst window), not the total alone.
* **Low exposure is not skill.** A calm or low-volatility filter holds cash in turbulent periods. In a falling market it beats buy-and-hold by being out, not by timing. Compare on risk as well as return: time in market, maximum loss inside a window, and return per unit of exposure.
* **Small sample.** 21 monthly windows over about 1.75 years is one market regime. A difference of a few percentage points is not statistically meaningful at this sample size. Treat the result as a sanity check of the paper trade, not as proof.
* **Fixed rule for all symbols.** If the original paper trade had different settings for each symbol, paste them as they were. Do not pick per-symbol settings now.

## 3. Why the settings must stay as they were before 2025

The purpose of this run is to answer one question: *would the calm rules, as I had them, have worked in 2025/26?* That question has a clean answer only if the rules and settings were fixed before the data existed.

* Any value changed after looking at 2025/26 bars, even "just fixing an obvious threshold", turns the test period into training data. The result then measures how well we fitted 2025/26, which is always flattering, and says nothing about the future.
* That is why `CALM_PARAMS_PRE2025` is a read-only mapping. Attempts to change it at runtime raise `TypeError`. The run manifest records the full settings of each strategy, together with `params_frozen_as_of = 2024-12-31`, so any later edit shows up as a difference between manifests.
* If you find a genuine **bug** (code that did not do what the pre-2025 rule said), fixing it is allowed. Write down the fix and the reason in your commit message **before** re-running, and keep the old result for comparison.
* If you want to try new settings, do it as a **new, separately named strategy** and set aside fresh data that neither version has seen (for example Q4 2026 onwards) to judge it. Never replace the frozen values in place.

## 4. Survivorship bias

* **Symbol choice.** Testing on the coins or stocks that are well known *today* (in October 2026) picks the winners. Use the same symbols that were in the calm paper trade before 2025, including any that have since fallen, been delisted or been renamed.
* **Delistings in the data.** Binance klines and stooq series simply stop when a pair or stock is delisted. A buy-and-hold position in a symbol that disappears mid-window cannot be valued at the window end. The data step should report missing tails, not fill them forward. Any symbol whose data stops early must be reported, not quietly dropped.
* **Index and ETF composition.** ETFs carry their own survivorship handling, which is fine. A hand-picked basket of single stocks does not.
* **Venue survivorship.** Binance spot prices are used as the reference for crypto. If you actually trade at a Danish broker or another exchange, prices, spreads and available pairs differ. The DKK conversion adds currency risk, which is identical across strategies within a run.

## 5. Decision: every strategy is sold at each window end (cash with cash)

**Decision:** in every window, every strategy, including buy-and-hold, starts with the same DKK cash and ends with **only cash**. Any open position is sold at the close of the last bar at or before the window end. Selling fees and slippage are charged on that sale.

Why:

1. **Like with like.** If buy-and-hold is valued at market while the calm strategy has already paid to get out, buy-and-hold is spared one set of exit costs. That biases the comparison in its favour. Selling everything puts every strategy on the same footing: realised DKK after all costs.
2. **It is what you asked for.** "Buy and hold should sell at the end of the window." Applying the same rule to every strategy keeps the only difference between runs the strategy itself.
3. **Independent windows.** Ending flat means no position, cost or indicator state carries into the next window (`reset()` is called at each start). Windows can be read one by one and counted.
4. **No next-open fill.** The sale happens at the **close** of the last bar inside the window, not at the next bar's open. The next open may lie after the window end (weekend, holiday, data gap) and would be look-ahead in the other direction.

Side effects to be aware of:

* Monthly windows mean at least one round trip per month for buy-and-hold. With real fees, that is a small fixed drag shared by all strategies that hold at the window end.
* A target of "hold" decided on the last bar is ignored, because the engine liquidates. A target decided on the second-to-last bar is filled at the last bar's open, and the position is then sold at that bar's close. This round trip within one bar is real cost, and it is kept.
* Checked by: `results/run_manifest.json` → `window_end_states[*].final_position == 0` (project check) and `tests/test_buy_hold.py` / `tests/test_run_parity.py`.

## 6. Fees and slippage are unverified

* `config/run_config.json` ships with `fee_bps = 0` and `fees_unverified = true`. Every result row and the report carry a fees-unverified warning while this flag is set.
* **Zero fees overstate every net return.** They overstate active strategies most: a calm filter that switches in and out several times a month pays fees on every switch. Buy-and-hold pays only two fees per window. Until real fees are entered, the gap between calm and buy-and-hold is biased **in favour of the calm strategy**.
* To fix this, take the numbers from **your broker's own published price list** (for example the venue's fee schedule page or price list PDF), not from a blog or a comparison site:
  * commission per trade (percentage and/or minimum in DKK; minimum fees matter a lot at small sizes);
  * currency conversion fee if the instrument is not priced in DKK (often 0.1–0.25 % at Danish brokers; confirm yours);
  * maker/taker fee for crypto at the venue you actually use, with your actual tier.
  Then set `fee_bps` (and any minimum fee the engine supports), set `fees_unverified = false`, and write the source and date of the price list in the settings file's note.
* **Slippage** is a model, not a measurement. Hourly open prices on liquid pairs (BTC/USDT, large ETFs) are well approximated by a few basis points. Small caps and thin pairs need more. Keep the same `slippage_bps` for all strategies (the parity test enforces this).
* **Taxes** (Danish rules on capital gains, the lagerbeskatning mark-to-market tax on many ETFs, and crypto taxed as personal income) are **not** modelled. They do not change the ranking within a window, but they do change the net DKK.

## 7. Checklist before trusting a 2025/26 number

- [ ] Original pre-2025 rules pasted into `CalmUserRules.decide`. Settings pasted into `CALM_PARAMS_PRE2025["calm_user"]` with `is_placeholder = false`. Done **before** looking at any 2025/26 result.
- [ ] Same symbols as the original paper trade, including any that have since been delisted.
- [ ] `scripts/fetch_data.py` run. Data label is `REAL`. `dataset_sha256` locked in `config/run_config.json`.
- [ ] Broker price list entered. `fees_unverified = false`. Source noted.
- [ ] `python -m pytest -q` passes (parity, DST, forced exit, hash check).
- [ ] Results read across all windows (median, share of windows won, worst window, time in market), not only the total.
