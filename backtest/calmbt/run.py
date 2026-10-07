"""CLI: python -m calmbt.run --config config/run_config.json --data data/x.csv --out results"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any, Dict, List

from . import data as dmod
from .config import config_without_strategy, interval_timedelta, load_config, thaw, windows_utc
from .engine import run_window
from .strategies import make_strategy

FEE_WARNING = "FEES UNVERIFIED: fee_bps is a placeholder; enter your broker's price list before trusting results"
COLUMNS = ["data_label", "strategy", "window", "start_dkk", "end_dkk", "pnl_dkk_after_costs",
           "return_pct", "fees_paid_dkk", "trades", "bars", "final_position", "status",
           "dataset_sha256", "warning"]


def run_all(cfg, bars, label: str, sha: str):
    wins = windows_utc(cfg)
    interval = interval_timedelta(cfg["bar_interval"])
    rows: List[Dict[str, Any]] = []
    ends: List[Dict[str, Any]] = []
    strategies = {}
    for name in cfg["strategies"]:
        strat = make_strategy(name)
        strategies[name] = strat.describe()
        for w in wins:
            r = run_window(strat, bars, w, cfg, interval)
            warn = []
            if cfg["fees_unverified"]:
                warn.append(FEE_WARNING)
            if getattr(strat, "is_placeholder", False):
                warn.append("PLACEHOLDER calm rules, not your original strategy")
            if r.status != "OK":
                warn.append("no bars in window")
            rows.append({
                "data_label": label, "strategy": name, "window": w.name,
                "start_dkk": round(r.start_dkk, 2), "end_dkk": round(r.end_dkk, 2),
                "pnl_dkk_after_costs": round(r.pnl_dkk, 2), "return_pct": round(r.return_pct, 4),
                "fees_paid_dkk": round(r.fees_dkk, 2), "trades": r.trades, "bars": r.bars,
                "final_position": r.final_position, "status": r.status,
                "dataset_sha256": sha, "warning": "; ".join(warn)})
            ends.append({"strategy": name, "window": w.name,
                         "window_start_local": w.start_local.isoformat(),
                         "window_end_local": w.end_local.isoformat(),
                         "window_start_utc": w.start_utc.isoformat(),
                         "window_end_utc": w.end_utc.isoformat(),
                         "final_position": r.final_position,
                         "final_cash_dkk": r.final_cash_dkk, "final_value_dkk": r.final_value_dkk,
                         "final_value_equals_cash": abs(r.final_value_dkk - r.final_cash_dkk) < 1e-9})
    return rows, ends, strategies, wins


def summarise(rows):
    out = {}
    for r in rows:
        s = out.setdefault(r["strategy"], {"pnl": 0.0, "fees": 0.0, "trades": 0, "end": 0.0, "n": 0})
        s["pnl"] += r["pnl_dkk_after_costs"]; s["fees"] += r["fees_paid_dkk"]
        s["trades"] += r["trades"]; s["n"] += 1
    return out


def write_md(path, rows, cfg, label, sha):
    L = [f"# {label} DATA - backtest results" + (" (synthetic, NOT market prices)" if label == "SAMPLE" else ""), ""]
    L.append(f"Data label: **{label}** | dataset sha256: `{sha}`")
    if cfg["fees_unverified"]:
        L += ["", f"> WARNING: {FEE_WARNING}."]
    L += ["", f"Every window starts with {cfg['starting_capital_dkk']:,.0f} DKK and ends in cash only "
          f"(forced sale at the close of the last bar). Fees {cfg['fee_bps']} bps, slippage "
          f"{cfg['slippage_bps']} bps, bars {cfg['bar_interval']}, tz {cfg['timezone']}.", "",
          "## Per strategy (sum over all windows, DKK, after costs)", "",
          "| strategy | windows | total PnL DKK | fees DKK | trades |", "|---|---|---|---|---|"]
    for n, s in summarise(rows).items():
        L.append(f"| {n} | {s['n']} | {s['pnl']:,.2f} | {s['fees']:,.2f} | {s['trades']} |")
    L += ["", "## One row per window per strategy", "",
          "| label | strategy | window | start DKK | end DKK | PnL DKK | return % | fees DKK | trades |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['data_label']} | {r['strategy']} | {r['window']} | {r['start_dkk']:,.2f} | "
                 f"{r['end_dkk']:,.2f} | {r['pnl_dkk_after_costs']:,.2f} | {r['return_pct']:.4f} | "
                 f"{r['fees_paid_dkk']:,.2f} | {r['trades']} |")
    if any("PLACEHOLDER" in r["warning"] for r in rows):
        L += ["", "Note: calm strategies run PLACEHOLDER rules (see calmbt/calm.py); they say nothing about your real strategy."]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="calmbt.run")
    ap.add_argument("--config", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="results")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        bars = dmod.load_bars(a.data)  # verifies sha vs meta
        sha = dmod.dataset_fingerprint(a.data)
        label = dmod.dataset_label(a.data)
        if sha != cfg["dataset_sha256"]:
            raise SystemExit(f"refusing: dataset sha256 {sha} != config dataset_sha256 {cfg['dataset_sha256']}. "
                             "Update the locked config deliberately if you froze a new dataset.")
        rows, ends, strategies, wins = run_all(cfg, bars, label, sha)
    except (dmod.DatasetIntegrityError, ValueError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "results.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader(); w.writerows(rows)
    write_md(os.path.join(a.out, "results.md"), rows, cfg, label, sha)
    manifest = {"data_label": label, "dataset_sha256": sha, "config_path": a.config,
                "config": thaw(cfg), "config_without_strategy": config_without_strategy(cfg),
                "fees_unverified": bool(cfg["fees_unverified"]),
                "windows": [{"name": x.name, "start_local": x.start_local.isoformat(),
                             "end_local": x.end_local.isoformat(), "start_utc": x.start_utc.isoformat(),
                             "end_utc": x.end_utc.isoformat()} for x in wins],
                "strategies": strategies, "window_end_states": ends}
    with open(os.path.join(a.out, "run_manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2); fh.write("\n")
    print(f"{label} data: wrote {len(rows)} rows to {a.out}/")
    for n, s in summarise(rows).items():
        print(f"  {n:22s} PnL {s['pnl']:>12,.2f} DKK  fees {s['fees']:>9,.2f}  trades {s['trades']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
