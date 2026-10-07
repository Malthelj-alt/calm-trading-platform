"""Locked run settings. Loaded once, deeply read-only."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, List, Mapping, NamedTuple, Tuple
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

REQUIRED_KEYS = (
    "timezone", "windows", "starting_capital_dkk", "fx_to_dkk", "fx_source_note",
    "fee_bps", "fees_unverified", "slippage_bps", "bar_interval", "dataset_sha256",
    "force_flat_at_window_end", "strategies",
)


class ConfigError(ValueError):
    pass


class Window(NamedTuple):
    name: str
    start_local: datetime  # tz-aware Europe/Copenhagen
    end_local: datetime
    start_utc: datetime
    end_utc: datetime


def _freeze(o: Any) -> Any:
    if isinstance(o, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in o.items()})
    if isinstance(o, (list, tuple)):
        return tuple(_freeze(v) for v in o)
    return o


def thaw(o: Any) -> Any:
    if isinstance(o, Mapping):
        return {k: thaw(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [thaw(v) for v in o]
    return o


def interval_timedelta(text: str) -> timedelta:
    units = {"m": 60, "h": 3600, "d": 86400}
    try:
        return timedelta(seconds=int(text[:-1]) * units[text[-1]])
    except (KeyError, ValueError, IndexError):
        raise ConfigError(f"bad bar_interval {text!r} (use e.g. '1h', '15m')")


def validate(raw: Mapping[str, Any]) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        raise ConfigError(f"config missing keys: {missing}")
    if raw["timezone"] != "Europe/Copenhagen":
        raise ConfigError("timezone must be Europe/Copenhagen")
    if raw["force_flat_at_window_end"] is not True:
        raise ConfigError("force_flat_at_window_end must be true (all results are cash after costs)")
    if not raw["windows"] or not raw["strategies"]:
        raise ConfigError("windows and strategies must be non-empty")
    if float(raw["starting_capital_dkk"]) <= 0 or float(raw["fx_to_dkk"]) <= 0:
        raise ConfigError("starting_capital_dkk and fx_to_dkk must be > 0")
    if float(raw["fee_bps"]) < 0 or float(raw["slippage_bps"]) < 0:
        raise ConfigError("fee_bps and slippage_bps must be >= 0")
    interval_timedelta(raw["bar_interval"])
    if len(set(raw["strategies"])) != len(raw["strategies"]):
        raise ConfigError("duplicate strategy names")


def load_config(path: str) -> Mapping[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    validate(raw)
    return _freeze(raw)


def _local(text: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        raise ConfigError(f"window time {text!r} must be Danish local time without offset")
    dt = dt.replace(tzinfo=tz)
    # Reject nonexistent (spring gap) / ambiguous (autumn fold) local times.
    rt = dt.astimezone(UTC).astimezone(tz)
    if rt.replace(tzinfo=None) != dt.replace(tzinfo=None):
        raise ConfigError(f"window time {text!r} does not exist in Europe/Copenhagen (DST gap)")
    if dt.replace(fold=1).utcoffset() != dt.utcoffset():
        raise ConfigError(f"window time {text!r} is ambiguous in Europe/Copenhagen (DST fold)")
    return dt


def windows_utc(cfg: Mapping[str, Any]) -> List[Window]:
    """Convert each Danish local window to UTC per date using zoneinfo (no fixed offset)."""
    tz = ZoneInfo(cfg["timezone"])
    out = []
    for w in cfg["windows"]:
        s, e = _local(w["start"], tz), _local(w["end"], tz)
        su, eu = s.astimezone(UTC), e.astimezone(UTC)
        if eu <= su:
            raise ConfigError(f"window {w['name']} ends before it starts")
        out.append(Window(w["name"], s, e, su, eu))
    for a, b in zip(out, out[1:]):
        if b.start_utc < a.end_utc:
            raise ConfigError(f"windows {a.name} and {b.name} overlap")
    return out


def config_without_strategy(cfg: Mapping[str, Any]) -> dict:
    """Plain-dict copy minus the strategy list: what must be identical across runs."""
    d = thaw(cfg)
    d.pop("strategies", None)
    return copy.deepcopy(d)
