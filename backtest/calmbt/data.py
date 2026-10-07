"""Frozen OHLCV dataset I/O in Danish time (Europe/Copenhagen).

File format
-----------
``<name>.csv`` — UTF-8, ``\\n`` line endings, header::

    ts_utc,ts_dk,open,high,low,close,volume

* ``ts_utc``  ISO-8601 bar *open* time in UTC, e.g. ``2025-03-30T01:00:00+00:00``.
  This column is authoritative.
* ``ts_dk``   the same instant in Europe/Copenhagen with its explicit offset,
  e.g. ``2025-03-30T03:00:00+02:00``. Stored with offset so the repeated hour on
  the autumn DST change (02:00–03:00 occurs twice) is unambiguous.
* prices / volume as plain decimals.

``<name>.csv.meta.json`` — provenance description. Required keys:
``label`` (``REAL`` or ``SAMPLE``), ``sha256`` (hex digest of the CSV bytes),
``rows``, ``source``, ``symbol``, ``interval``, ``first_ts_utc``,
``last_ts_utc``, ``timezone``.

:func:`load_bars` refuses to load a CSV whose SHA-256 does not match its meta,
so a backtest can never silently run on an edited or swapped dataset.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

DK_TZ_NAME = "Europe/Copenhagen"
DK_TZ = ZoneInfo(DK_TZ_NAME)
UTC = timezone.utc

CSV_HEADER = ["ts_utc", "ts_dk", "open", "high", "low", "close", "volume"]
VALID_LABELS = ("REAL", "SAMPLE")
REQUIRED_META_KEYS = (
    "label",
    "sha256",
    "rows",
    "source",
    "symbol",
    "interval",
    "first_ts_utc",
    "last_ts_utc",
    "timezone",
)
META_SUFFIX = ".meta.json"


class DatasetIntegrityError(Exception):
    """Raised when a dataset is missing its meta, fails the hash check or is malformed."""


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ``ts_utc``/``ts_dk`` are tz-aware and denote the bar OPEN time."""

    ts_utc: datetime
    ts_dk: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    return dt.astimezone(UTC)


def to_dk(dt: datetime) -> datetime:
    """Convert a tz-aware datetime to Europe/Copenhagen (DST handled by zoneinfo)."""
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    return dt.astimezone(DK_TZ)


def utc_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def format_ts(dt: datetime) -> str:
    """ISO-8601 with seconds and explicit offset (stable, round-trippable)."""
    return dt.isoformat(timespec="seconds")


def parse_ts(text: str) -> datetime:
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without offset: {text!r}")
    return dt


def make_bar(ts_utc: datetime, o: float, h: float, l: float, c: float, v: float) -> Bar:
    u = to_utc(ts_utc)
    return Bar(u, to_dk(u), float(o), float(h), float(l), float(c), float(v))


# --------------------------------------------------------------------------- #
# Hashing / meta
# --------------------------------------------------------------------------- #
def meta_path(path: str) -> str:
    return str(path) + META_SUFFIX


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dataset_fingerprint(path: str) -> str:
    """SHA-256 hex digest of the dataset CSV bytes (what the run config locks to)."""
    return sha256_file(path)


def load_meta(path: str) -> Dict[str, Any]:
    """Load ``<path>.meta.json``; raises DatasetIntegrityError if missing/invalid."""
    mp = meta_path(path)
    if not os.path.exists(mp):
        raise DatasetIntegrityError(
            f"missing dataset description {mp!r}; refusing to load {path!r} without provenance"
        )
    try:
        with open(mp, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as exc:
        raise DatasetIntegrityError(f"cannot read meta {mp!r}: {exc}") from exc
    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    if missing:
        raise DatasetIntegrityError(f"meta {mp!r} missing keys: {missing}")
    if meta["label"] not in VALID_LABELS:
        raise DatasetIntegrityError(
            f"meta {mp!r} has label {meta['label']!r}; expected one of {VALID_LABELS}"
        )
    return meta


def dataset_label(path: str) -> str:
    """``'REAL'`` or ``'SAMPLE'`` from the dataset meta."""
    return str(load_meta(path)["label"])


def verify_dataset(path: str) -> Dict[str, Any]:
    """Check the CSV hash against its meta. Returns the meta on success."""
    if not os.path.exists(path):
        raise DatasetIntegrityError(f"dataset not found: {path!r}")
    meta = load_meta(path)
    actual = sha256_file(path)
    expected = str(meta["sha256"]).lower()
    if actual != expected:
        raise DatasetIntegrityError(
            f"SHA-256 mismatch for {path!r}: file={actual} meta={expected}. "
            "The dataset was modified after it was frozen; refusing to load."
        )
    return meta


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def _f(value: str, col: str, lineno: int) -> float:
    try:
        x = float(value)
    except ValueError as exc:
        raise DatasetIntegrityError(f"line {lineno}: bad {col}={value!r}") from exc
    if not math.isfinite(x):
        raise DatasetIntegrityError(f"line {lineno}: non-finite {col}={value!r}")
    return x


def load_bars(path: str, verify: bool = True) -> List[Bar]:
    """Load a frozen dataset.

    Refuses to load (raises :class:`DatasetIntegrityError`) when:
    * the meta file is missing or invalid,
    * the CSV SHA-256 differs from ``meta['sha256']``,
    * the header is wrong, timestamps are unsorted/duplicated,
    * ``ts_dk`` is not exactly ``ts_utc`` converted to Europe/Copenhagen,
    * OHLC values are inconsistent (high < max(open, close) etc.),
    * the row count differs from ``meta['rows']``.

    ``verify=False`` skips only the hash/meta check (used by the writers
    for self-checks); never use it in a backtest.
    """
    meta: Optional[Dict[str, Any]] = verify_dataset(path) if verify else None

    bars: List[Bar] = []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != CSV_HEADER:
            raise DatasetIntegrityError(f"bad header {header!r}; expected {CSV_HEADER!r}")
        prev: Optional[datetime] = None
        for lineno, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) != len(CSV_HEADER):
                raise DatasetIntegrityError(f"line {lineno}: expected 7 columns, got {len(row)}")
            try:
                ts_utc = parse_ts(row[0]).astimezone(UTC)
                ts_dk_file = parse_ts(row[1])
            except ValueError as exc:
                raise DatasetIntegrityError(f"line {lineno}: {exc}") from exc
            ts_dk = to_dk(ts_utc)
            # NB: compare UTC instants + offsets, not the datetimes themselves:
            # PEP 495 makes inter-zone == always False for times in the DST
            # fold (e.g. 2025-10-26 02:00 DK), even when they are the same instant.
            if (
                ts_dk_file.astimezone(UTC) != ts_utc
                or ts_dk_file.utcoffset() != ts_dk.utcoffset()
            ):
                raise DatasetIntegrityError(
                    f"line {lineno}: ts_dk {row[1]!r} is not ts_utc in {DK_TZ_NAME} "
                    f"(expected {format_ts(ts_dk)})"
                )
            if prev is not None and ts_utc <= prev:
                raise DatasetIntegrityError(
                    f"line {lineno}: timestamps not strictly increasing ({row[0]})"
                )
            prev = ts_utc
            o = _f(row[2], "open", lineno)
            h = _f(row[3], "high", lineno)
            l = _f(row[4], "low", lineno)
            c = _f(row[5], "close", lineno)
            v = _f(row[6], "volume", lineno)
            if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h) or v < 0:
                raise DatasetIntegrityError(f"line {lineno}: inconsistent OHLCV {row[2:]}")
            bars.append(Bar(ts_utc, ts_dk, o, h, l, c, v))

    if meta is not None and int(meta["rows"]) != len(bars):
        raise DatasetIntegrityError(
            f"row count {len(bars)} != meta rows {meta['rows']} for {path!r}"
        )
    return bars


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #
def _num(x: float) -> str:
    # repr() of a float is the shortest round-trippable form: deterministic.
    x = float(x)
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(x)


def normalize_bars(bars: Iterable[Bar]) -> List[Bar]:
    """Sort by UTC time and de-duplicate (last occurrence of a timestamp wins)."""
    by_ts: Dict[datetime, Bar] = {}
    for b in bars:
        by_ts[to_utc(b.ts_utc)] = b
    return [by_ts[k] for k in sorted(by_ts)]


def render_csv(bars: List[Bar]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for b in bars:
        u = to_utc(b.ts_utc)
        w.writerow(
            [
                format_ts(u),
                format_ts(to_dk(u)),
                _num(b.open),
                _num(b.high),
                _num(b.low),
                _num(b.close),
                _num(b.volume),
            ]
        )
    return buf.getvalue().encode("utf-8")


def write_dataset(path: str, bars: Iterable[Bar], meta: Dict[str, Any]) -> Dict[str, Any]:
    """Sort/dedupe ``bars``, write the CSV and its ``.meta.json``; return the full meta.

    ``meta`` must contain at least ``label``, ``source``, ``symbol``, ``interval``.
    ``sha256``, ``rows``, ``first/last_ts_*`` and ``timezone`` are filled in here.
    """
    label = meta.get("label")
    if label not in VALID_LABELS:
        raise ValueError(f"label must be one of {VALID_LABELS}, got {label!r}")
    clean = normalize_bars(bars)
    if not clean:
        raise ValueError("refusing to write an empty dataset")
    data = render_csv(clean)

    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)

    full = dict(meta)
    full.update(
        {
            "file": os.path.basename(str(path)),
            "format": "csv",
            "columns": CSV_HEADER,
            "timestamp_convention": "bar open time; ts_utc authoritative, ts_dk = ts_utc in Europe/Copenhagen",
            "timezone": DK_TZ_NAME,
            "rows": len(clean),
            "first_ts_utc": format_ts(to_utc(clean[0].ts_utc)),
            "last_ts_utc": format_ts(to_utc(clean[-1].ts_utc)),
            "first_ts_dk": format_ts(to_dk(clean[0].ts_utc)),
            "last_ts_dk": format_ts(to_dk(clean[-1].ts_utc)),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    with open(meta_path(path), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(full, fh, indent=2, sort_keys=True)
        fh.write("\n")

    # Self-check: what we wrote must load cleanly through the strict path.
    load_bars(path, verify=True)
    return full


__all__ = [
    "Bar",
    "CSV_HEADER",
    "DK_TZ",
    "DK_TZ_NAME",
    "DatasetIntegrityError",
    "dataset_fingerprint",
    "dataset_label",
    "format_ts",
    "load_bars",
    "load_meta",
    "make_bar",
    "meta_path",
    "normalize_bars",
    "parse_ts",
    "render_csv",
    "sha256_file",
    "to_dk",
    "to_utc",
    "utc_from_ms",
    "verify_dataset",
    "write_dataset",
]
