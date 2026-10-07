"""Append-only SQLite ledger.

Tables: runs, tests, fills, trades. Money is stored as TEXT holding the exact
Decimal string (convert with ``Decimal(value)``); percentages are percent units
(e.g. "2.5" means 2.5 %). Fills and trades can never be updated or deleted.
A test row and a run row may be finalised exactly once and are then frozen.

The canonical ledger hash (sha256) covers the run configuration, the data
fingerprint, every test result, fill and trade. It leaves out row ids, the
local file path and anything wall-clock based, so replaying the same data
gives the same hash in any database.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile         TEXT    NOT NULL,
    fee_rate        TEXT    NOT NULL,
    slippage_rate   TEXT    NOT NULL,
    start_balance   TEXT    NOT NULL,
    instrument      TEXT    NOT NULL,
    interval        TEXT    NOT NULL,
    data_source     TEXT    NOT NULL,
    data_path       TEXT,
    data_sha256     TEXT    NOT NULL,
    bar_count       INTEGER NOT NULL,
    warmup_bars     INTEGER NOT NULL,
    eval_bars       INTEGER NOT NULL,
    eval_start_ms   INTEGER NOT NULL,
    eval_end_ms     INTEGER NOT NULL,
    seed            INTEGER NOT NULL,
    engine_version  TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'running',
    ledger_hash     TEXT
);

CREATE TABLE IF NOT EXISTS tests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(id),
    number           INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    strategy_key     TEXT    NOT NULL,
    description      TEXT    NOT NULL,
    start_balance    TEXT    NOT NULL,
    final_balance    TEXT,
    pnl              TEXT,
    pnl_pct          TEXT,
    trade_count      INTEGER,
    win_rate         TEXT,
    max_drawdown_pct TEXT,
    fees_paid        TEXT,
    UNIQUE (run_id, number)
);

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    test_id         INTEGER NOT NULL REFERENCES tests(id),
    seq             INTEGER NOT NULL,
    side            TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    reason          TEXT    NOT NULL CHECK (reason IN ('signal', 'final_close')),
    signal_index    INTEGER NOT NULL,
    signal_time_ms  INTEGER NOT NULL,
    fill_index      INTEGER NOT NULL,
    fill_time_ms    INTEGER NOT NULL,
    ref_price       TEXT    NOT NULL,
    price           TEXT    NOT NULL,
    qty             TEXT    NOT NULL,
    notional        TEXT    NOT NULL,
    fee             TEXT    NOT NULL,
    cash_after      TEXT    NOT NULL,
    position_after  TEXT    NOT NULL,
    data_source     TEXT    NOT NULL,
    UNIQUE (test_id, seq)
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    test_id         INTEGER NOT NULL REFERENCES tests(id),
    seq             INTEGER NOT NULL,
    entry_time_ms   INTEGER NOT NULL,
    entry_price     TEXT    NOT NULL,
    exit_time_ms    INTEGER NOT NULL,
    exit_price      TEXT    NOT NULL,
    qty             TEXT    NOT NULL,
    cost            TEXT    NOT NULL,
    proceeds        TEXT    NOT NULL,
    fees            TEXT    NOT NULL,
    pnl             TEXT    NOT NULL,
    pnl_pct         TEXT    NOT NULL,
    exit_reason     TEXT    NOT NULL,
    data_source     TEXT    NOT NULL,
    UNIQUE (test_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_tests_run  ON tests(run_id);
CREATE INDEX IF NOT EXISTS idx_fills_test ON fills(test_id);
CREATE INDEX IF NOT EXISTS idx_trades_test ON trades(test_id);

CREATE TRIGGER IF NOT EXISTS fills_no_update BEFORE UPDATE ON fills
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: fills cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS fills_no_delete BEFORE DELETE ON fills
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: fills cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS trades_no_update BEFORE UPDATE ON trades
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: trades cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS trades_no_delete BEFORE DELETE ON trades
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: trades cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS tests_no_delete BEFORE DELETE ON tests
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: tests cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS tests_frozen BEFORE UPDATE ON tests
WHEN OLD.final_balance IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: finished tests are frozen'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: runs cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS runs_frozen BEFORE UPDATE ON runs
WHEN OLD.status = 'complete'
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: completed runs are frozen'); END;
"""

_FILL_COLS = (
    "seq", "side", "reason", "signal_index", "signal_time_ms", "fill_index", "fill_time_ms",
    "ref_price", "price", "qty", "notional", "fee", "cash_after", "position_after", "data_source",
)
_TRADE_COLS = (
    "seq", "entry_time_ms", "entry_price", "exit_time_ms", "exit_price", "qty", "cost",
    "proceeds", "fees", "pnl", "pnl_pct", "exit_reason", "data_source",
)
_TEST_RESULT_COLS = (
    "final_balance", "pnl", "pnl_pct", "trade_count", "win_rate", "max_drawdown_pct", "fees_paid",
)
_RUN_HASHED_COLS = (
    "profile", "fee_rate", "slippage_rate", "start_balance", "instrument", "interval",
    "data_source", "data_sha256", "bar_count", "warmup_bars", "eval_bars",
    "eval_start_ms", "eval_end_ms", "seed", "engine_version",
)


class LedgerOpenError(OSError):
    """The ledger database file could not be opened or created."""


def _db_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    return v


def connect(db_path: str) -> sqlite3.Connection:
    """Open (and create if needed) a ledger database.

    Raises LedgerOpenError (an OSError) naming the path when the file or its
    directory cannot be opened/created, instead of sqlite's bare message.
    """
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError as exc:
        raise LedgerOpenError(
            "cannot open ledger database %r (%s); check that the directory exists "
            "and is writable" % (db_path, exc)) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class Ledger:
    """Thin writer over the SQLite schema. Used by the JournalAgent."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, db_path: str) -> "Ledger":
        return cls(connect(db_path))

    def create_run(self, **cfg: Any) -> int:
        cols = list(cfg)
        cur = self.conn.execute(
            "INSERT INTO runs (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
            [_db_value(cfg[c]) for c in cols],
        )
        return int(cur.lastrowid)

    def create_test(self, run_id: int, number: int, name: str, strategy_key: str,
                    description: str, start_balance: Decimal) -> int:
        cur = self.conn.execute(
            "INSERT INTO tests (run_id, number, name, strategy_key, description, start_balance)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, number, name, strategy_key, description, str(start_balance)),
        )
        return int(cur.lastrowid)

    def append_fill(self, run_id: int, test_id: int, row: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO fills (run_id, test_id, %s) VALUES (?, ?, %s)"
            % (", ".join(_FILL_COLS), ", ".join("?" * len(_FILL_COLS))),
            [run_id, test_id] + [_db_value(row[c]) for c in _FILL_COLS],
        )
        return int(cur.lastrowid)

    def append_trade(self, run_id: int, test_id: int, row: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO trades (run_id, test_id, %s) VALUES (?, ?, %s)"
            % (", ".join(_TRADE_COLS), ", ".join("?" * len(_TRADE_COLS))),
            [run_id, test_id] + [_db_value(row[c]) for c in _TRADE_COLS],
        )
        return int(cur.lastrowid)

    def finalize_test(self, test_id: int, stats: Dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE tests SET %s WHERE id = ? AND final_balance IS NULL"
            % ", ".join("%s = ?" % c for c in _TEST_RESULT_COLS),
            [_db_value(stats[c]) for c in _TEST_RESULT_COLS] + [test_id],
        )

    def finalize_run(self, run_id: int, ledger_hash: str) -> None:
        self.conn.execute(
            "UPDATE runs SET status = 'complete', ledger_hash = ? WHERE id = ?",
            (ledger_hash, run_id),
        )

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()


# ------------------------------------------------------------------ canonical hash
def canonical_ledger(conn: sqlite3.Connection, run_id: int) -> Dict[str, Any]:
    """The run as a plain, id-free structure used for hashing and comparison."""
    conn.row_factory = sqlite3.Row
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise KeyError("run %r not found" % run_id)
    tests = conn.execute(
        "SELECT * FROM tests WHERE run_id = ? ORDER BY number", (run_id,)).fetchall()
    numbers = {t["id"]: t["number"] for t in tests}
    out_tests = []
    for t in tests:
        d = {"number": t["number"], "name": t["name"], "strategy_key": t["strategy_key"],
             "start_balance": t["start_balance"]}
        d.update({c: t[c] for c in _TEST_RESULT_COLS})
        out_tests.append(d)

    def rows(table: str, cols: Iterable[str]) -> List[Dict[str, Any]]:
        res = conn.execute(
            "SELECT * FROM %s WHERE run_id = ? ORDER BY test_id, seq" % table, (run_id,)).fetchall()
        items = []
        for r in res:
            d = {"test": numbers[r["test_id"]]}
            d.update({c: r[c] for c in cols})
            items.append(d)
        items.sort(key=lambda d: (d["test"], d["seq"]))
        return items

    return {
        "run": {c: run[c] for c in _RUN_HASHED_COLS},
        "tests": out_tests,
        "fills": rows("fills", _FILL_COLS),
        "trades": rows("trades", _TRADE_COLS),
    }


def compute_ledger_hash(conn: sqlite3.Connection, run_id: int) -> str:
    blob = json.dumps(canonical_ledger(conn, run_id), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def ledger_hash(db_path: str, run_id: int) -> str:
    """Recompute the hash of a stored run straight from the database."""
    conn = connect(db_path)
    try:
        return compute_ledger_hash(conn, run_id)
    finally:
        conn.close()


# ------------------------------------------------------------------ read helpers
def latest_runs(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    """Latest completed run per profile, e.g. {'base': row, 'stress': row}."""
    conn.row_factory = sqlite3.Row
    out: Dict[str, sqlite3.Row] = {}
    for r in conn.execute(
            "SELECT * FROM runs WHERE status = 'complete' ORDER BY id"):
        out[r["profile"]] = r
    return out


def run_tests(conn: sqlite3.Connection, run_id: int) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM tests WHERE run_id = ? ORDER BY number", (run_id,)).fetchall()


def test_trades(conn: sqlite3.Connection, test_id: int) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM trades WHERE test_id = ? ORDER BY seq", (test_id,)).fetchall()


def test_fills(conn: sqlite3.Connection, test_id: int) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM fills WHERE test_id = ? ORDER BY seq", (test_id,)).fetchall()


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
