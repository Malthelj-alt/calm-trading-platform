"""Every strategy must be run under identical settings and end every window flat."""
from __future__ import annotations

import json
import os

import pytest

from calmbt import run as runmod
from calmbt.config import config_without_strategy, interval_timedelta, thaw
from conftest import frozen, make_cfg, hourly_bars, utc, wiggly_price


def strategy_less(settings: dict) -> dict:
    d = json.loads(json.dumps(settings))
    d.pop("strategies", None)
    d.pop("strategy", None)
    return d


def settings_per_strategy(manifest: dict) -> dict:
    """The settings each strategy ran under: the locked config narrowed to that one strategy."""
    out = {}
    for name in manifest["config"]["strategies"]:
        s = json.loads(json.dumps(manifest["config"]))
        s["strategies"] = [name]
        out[name] = s
    return out


@pytest.fixture
def manifest(tmp_path, cli_config):
    cfg_path, data_path, _ = cli_config
    out = str(tmp_path / "out")
    assert runmod.main(["--config", cfg_path, "--data", data_path, "--out", out]) == 0
    with open(os.path.join(out, "run_manifest.json")) as fh:
        return json.load(fh)


def test_settings_identical_apart_from_strategy(manifest):
    per = settings_per_strategy(manifest)
    assert len(per) >= 2
    stripped = {n: strategy_less(s) for n, s in per.items()}
    first = next(iter(stripped.values()))
    for n, s in stripped.items():
        assert s == first, f"settings for {n} differ from the others"
    assert first == manifest["config_without_strategy"]


def test_parity_check_catches_a_key_added_later(manifest):
    """The comparison is over whole dicts, so any new key that differs must fail it."""
    per = settings_per_strategy(manifest)
    names = list(per)
    per[names[1]]["some_future_key"] = 1
    stripped = [strategy_less(s) for s in per.values()]
    assert stripped[0] != stripped[1]


def test_engine_receives_identical_settings_for_every_strategy(monkeypatch, tmp_path, one_month_windows):
    """Record what the engine is called with; add an unknown key to prove it is also compared."""
    cfg = make_cfg(one_month_windows, brand_new_setting={"nested": [1, 2]}, fee_bps=7.0)
    bars = hourly_bars(utc(2025, 1, 9, 12), 24 * 6, wiggly_price) + hourly_bars(utc(2025, 7, 9, 12), 24 * 6, wiggly_price)
    calls = []
    real = runmod.run_window

    def spy(strategy, b, w, c, interval):
        calls.append((strategy.name, thaw(c), w, interval))
        return real(strategy, b, w, c, interval)

    monkeypatch.setattr(runmod, "run_window", spy)
    runmod.run_all(frozen(cfg), bars, "SAMPLE", "0" * 64)
    names = {c[0] for c in calls}
    assert names == set(cfg["strategies"])
    ref_cfg = strategy_less(calls[0][1])
    assert ref_cfg["brand_new_setting"] == {"nested": [1, 2]}
    for name, c, w, interval in calls:
        assert strategy_less(c) == ref_cfg, name
        assert interval == interval_timedelta("1h")
    by_strategy = {n: [(w.name, w.start_utc, w.end_utc) for nn, _, w, _ in calls if nn == n] for n in names}
    ref = next(iter(by_strategy.values()))
    assert len(ref) == len(one_month_windows)
    assert all(v == ref for v in by_strategy.values())


def test_same_windows_dataset_and_capital_for_all(manifest):
    states = manifest["window_end_states"]
    names = list(manifest["strategies"])
    assert len(names) >= 2
    windows = {n: [(s["window"], s["window_start_utc"], s["window_end_utc"], s["window_start_local"],
                    s["window_end_local"]) for s in states if s["strategy"] == n] for n in names}
    ref = windows[names[0]]
    assert len(ref) == len(manifest["windows"]) > 0
    assert all(w == ref for w in windows.values())
    assert manifest["dataset_sha256"] == manifest["config"]["dataset_sha256"]
    # one dataset hash and one starting capital recorded for the whole run
    assert len(manifest["dataset_sha256"]) == 64


def test_rows_carry_same_hash_and_capital_per_strategy(tmp_path, cli_config):
    import csv
    cfg_path, data_path, cfg = cli_config
    out = str(tmp_path / "o")
    assert runmod.main(["--config", cfg_path, "--data", data_path, "--out", out]) == 0
    rows = list(csv.DictReader(open(os.path.join(out, "results.csv"))))
    assert len(rows) == len(cfg["strategies"]) * len(cfg["windows"])
    assert {r["dataset_sha256"] for r in rows} == {cfg["dataset_sha256"]}
    assert {float(r["start_dkk"]) for r in rows} == {cfg["starting_capital_dkk"]}
    assert {r["data_label"] for r in rows} == {"SAMPLE"}
    assert all("FEES UNVERIFIED" in r["warning"] for r in rows)


def test_every_strategy_holds_nothing_at_every_window_end(manifest):
    states = manifest["window_end_states"]
    assert {s["strategy"] for s in states} == set(manifest["strategies"])
    assert states
    for s in states:
        assert s["final_position"] == 0, s
        assert s["final_value_equals_cash"] is True, s


def test_calm_strategies_actually_trade_in_fixture(monkeypatch, one_month_windows):
    """Guard against a vacuous flat check: at least one non-buy-hold strategy must have traded."""
    cfg = make_cfg(one_month_windows)
    bars = hourly_bars(utc(2025, 1, 9, 12), 24 * 6, wiggly_price) + hourly_bars(utc(2025, 7, 9, 12), 24 * 6, wiggly_price)
    rows, ends, _, _ = runmod.run_all(frozen(cfg), bars, "SAMPLE", "0" * 64)
    assert any(r["trades"] > 0 for r in rows if r["strategy"] != "buy_hold")
    assert all(e["final_position"] == 0 for e in ends)


def test_refuses_dataset_hash_not_in_config(tmp_path, cli_config):
    cfg_path, data_path, cfg = cli_config
    cfg = dict(cfg, dataset_sha256="f" * 64)
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(cfg))
    with pytest.raises(SystemExit):
        runmod.main(["--config", str(p), "--data", data_path, "--out", str(tmp_path / "x")])
