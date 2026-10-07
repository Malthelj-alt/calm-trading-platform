"""Data loading refuses a file that does not match its description file; the future is poisoned."""
from __future__ import annotations

import dataclasses
import json
import math
import os
from datetime import timedelta

import pytest

from calmbt import data as dmod
from calmbt import run as runmod
from calmbt.config import interval_timedelta, windows_utc
from calmbt.engine import run_window
from calmbt.strategies import make_strategy
from conftest import frozen, hourly_bars, make_cfg, utc, wiggly_price


def test_clean_dataset_loads(frozen_dataset):
    path, meta, bars = frozen_dataset
    loaded = dmod.load_bars(path)
    assert len(loaded) == len(bars) == meta["rows"]
    assert dmod.dataset_fingerprint(path) == meta["sha256"]
    assert dmod.dataset_label(path) == "SAMPLE"


def test_refuses_csv_changed_after_freezing(frozen_dataset):
    path, _, _ = frozen_dataset
    raw = open(path, "rb").read()
    open(path, "wb").write(raw.replace(b"100.", b"101.", 1))
    with pytest.raises(dmod.DatasetIntegrityError, match="SHA-256 mismatch"):
        dmod.load_bars(path)


def test_refuses_when_meta_sha_is_wrong(frozen_dataset):
    path, _, _ = frozen_dataset
    mp = dmod.meta_path(path)
    meta = json.load(open(mp))
    meta["sha256"] = "0" * 64
    json.dump(meta, open(mp, "w"))
    with pytest.raises(dmod.DatasetIntegrityError, match="mismatch"):
        dmod.load_bars(path)


def test_refuses_missing_meta(frozen_dataset):
    path, _, _ = frozen_dataset
    os.remove(dmod.meta_path(path))
    with pytest.raises(dmod.DatasetIntegrityError, match="missing dataset description"):
        dmod.load_bars(path)


def test_refuses_appended_row_even_with_updated_row_count(frozen_dataset):
    path, _, _ = frozen_dataset
    with open(path, "ab") as fh:
        fh.write(b"2030-01-01T00:00:00+00:00,2030-01-01T01:00:00+01:00,1,2,1,2,3\n")
    with pytest.raises(dmod.DatasetIntegrityError):
        dmod.load_bars(path)


def test_cli_exits_2_on_tampered_data(tmp_path, frozen_dataset, cli_config, capsys):
    cfg_path, path, _ = cli_config
    open(path, "ab").write(b"\n")
    assert runmod.main(["--config", cfg_path, "--data", path, "--out", str(tmp_path / "o")]) == 2
    assert "mismatch" in capsys.readouterr().err
    assert not os.path.exists(tmp_path / "o" / "results.csv")


def test_dst_fold_hour_round_trips(tmp_path):
    bars = hourly_bars(utc(2025, 10, 25, 20), 12)  # covers the repeated 02:00 Danish hour
    p = str(tmp_path / "fold.csv")
    dmod.write_dataset(p, bars, {"label": "SAMPLE", "source": "t", "symbol": "T", "interval": "1h"})
    loaded = dmod.load_bars(p)
    assert [b.ts_utc for b in loaded] == [b.ts_utc for b in bars]
    assert [b.ts_dk.hour for b in loaded].count(2) == 2


# ---- future-bar poisoning (NaN and finite extremes) -------------------------

def _poison(bar, kind):
    v = {"nan": math.nan, "huge": 1e300, "tiny": 1e-300, "neg": -1e9}[kind]
    return dataclasses.replace(bar, open=v, high=v, low=v, close=v, volume=v)


@pytest.mark.parametrize("name", ["calm_vol_placeholder", "calm_user"])
@pytest.mark.parametrize("kind", ["nan", "huge", "tiny", "neg"])
def test_fills_up_to_cut_do_not_depend_on_future_bars(name, kind):
    cfg = make_cfg([{"name": "w", "start": "2025-01-10T00:00:00", "end": "2025-01-14T00:00:00"}],
                   fee_bps=10.0, slippage_bps=5.0)
    w = windows_utc(frozen(cfg))[0]
    interval = interval_timedelta("1h")
    clean = hourly_bars(w.start_utc, 96, wiggly_price)
    k = 70  # bars after index k are replaced by poison
    dirty = clean[:k + 1] + [_poison(b, kind) for b in clean[k + 1:]]
    a = run_window(make_strategy(name), clean, w, frozen(cfg), interval)
    b = run_window(make_strategy(name), dirty, w, frozen(cfg), interval)
    cutoff = clean[k].ts_utc.isoformat()
    fa = [f for f in a.fills if f["ts_utc"] <= cutoff]
    fb = [f for f in b.fills if f["ts_utc"] <= cutoff]
    assert fa, "fixture must trade before the cut, otherwise this test proves nothing"
    assert fa == fb
