"""Deterministic paper-trading engine.

Clock: the bar timestamps. With n bars, the evaluation window is the last 720
bars (indices eval_start..n-1, eval_start = max(200, n-720)); everything before
it is warm-up. Decisions are made at the close of bars eval_start-1 .. n-2 and
fill at the open of the next bar (t+1), so the whole window is tradable:
Buy & Hold buys at open[eval_start] and is closed at close[n-1].

All ten tests run side by side over the same clock, bars, $50 start and fee
profile; only the strategy differs.
"""
from __future__ import annotations

import decimal
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple, Union

from . import __version__
from .agents import (Bar, DataAgent, Fill, JournalAgent, Order, PaperExecutionAgent,
                     RiskAgent, StrategyAgent)
from .ledger import Ledger, connect
from .strategies import SEED, STRATEGIES, StrategySpec

START_BALANCE = Decimal("50")
WARMUP_BARS = 200
EVAL_BARS = 720
INSTRUMENT = "BTCUSDT"
INTERVAL = "1h"
HUNDRED = Decimal("100")
ZERO = Decimal("0")


@dataclass(frozen=True)
class Profile:
    name: str
    fee_rate: Decimal
    slippage_rate: Decimal

    @property
    def label(self) -> str:
        return "%s fee %s%% + slippage %s%% per side" % (
            self.name.capitalize(), _pct_str(self.fee_rate), _pct_str(self.slippage_rate))


def _pct_str(rate: Decimal) -> str:
    return "{:.2f}".format(rate * 100)


PROFILES: Dict[str, Profile] = {
    "base": Profile("base", Decimal("0.001"), Decimal("0.0005")),
    "stress": Profile("stress", Decimal("0.004"), Decimal("0.0005")),
}


def get_profile(profile: Union[str, Profile]) -> Profile:
    if isinstance(profile, Profile):
        return profile
    try:
        return PROFILES[profile]
    except KeyError:
        raise ValueError("unknown profile %r (choose from %s)" % (profile, ", ".join(PROFILES)))


def _context() -> decimal.Context:
    # Fixed precision for replayability. NaN/Inf traps are off so that garbage
    # in *future* bars can never crash decisions that were already made.
    ctx = decimal.Context(prec=28, rounding=decimal.ROUND_HALF_EVEN)
    ctx.traps[decimal.InvalidOperation] = False
    ctx.traps[decimal.DivisionByZero] = False
    return ctx


# --------------------------------------------------------------------- results
@dataclass
class TestResult:
    test_id: int
    number: int
    name: str
    strategy_key: str
    start_balance: Decimal
    final_balance: Decimal = ZERO
    pnl: Decimal = ZERO
    pnl_pct: Decimal = ZERO
    trade_count: int = 0
    win_rate: Optional[Decimal] = None
    max_drawdown_pct: Decimal = ZERO
    fees_paid: Decimal = ZERO
    signals: List[Tuple[int, int, str]] = field(default_factory=list)   # (t, time_ms, signal)
    fills: List[Fill] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)


@dataclass
class SimulationResult:
    run_id: int
    ledger_hash: str
    profile: Profile
    data_source: str
    eval_start: int
    eval_end: int
    tests: List[TestResult]

    def by_name(self, name: str) -> TestResult:
        for t in self.tests:
            if t.name == name:
                return t
        raise KeyError(name)


class _Book:
    """Per-test account: cash, position and the open trade."""

    def __init__(self, start: Decimal):
        self.cash = start
        self.qty = ZERO
        self.entry: Optional[Fill] = None
        self.pending: Optional[Order] = None
        self.peak = start
        self.max_dd = ZERO
        self.fees = ZERO

    def mark(self, equity: Decimal) -> None:
        if equity > self.peak:
            self.peak = equity
        if self.peak > ZERO:
            dd = (self.peak - equity) / self.peak
            if dd > self.max_dd:
                self.max_dd = dd


# --------------------------------------------------------------------- engine
def simulate(data: DataAgent, profile: Union[str, Profile] = "base",
             db: Union[str, Ledger, None] = None, data_path: Optional[str] = None,
             strategies: Sequence[StrategySpec] = STRATEGIES) -> SimulationResult:
    """Run every strategy side by side and journal the result.

    ``db`` may be a path, an open Ledger, or None (in-memory SQLite). Returns the
    in-memory results as well, which the tests use for poisoned-future checks.
    """
    prof = get_profile(profile)
    n = len(data)
    if n < WARMUP_BARS + 2:
        raise ValueError("need at least %d bars, got %d" % (WARMUP_BARS + 2, n))
    eval_start = max(WARMUP_BARS, n - EVAL_BARS)
    last = n - 1

    own_ledger = not isinstance(db, Ledger)
    ledger = db if isinstance(db, Ledger) else Ledger(connect(db or ":memory:"))
    try:
        with decimal.localcontext(_context()):
            result = _run(data, prof, ledger, data_path, strategies, eval_start, last)
        ledger.commit()
        return result
    except BaseException:
        ledger.rollback()
        raise
    finally:
        if own_ledger and db is not None:
            ledger.close()


def _run(data: DataAgent, prof: Profile, ledger: Ledger, data_path: Optional[str],
         strategies: Sequence[StrategySpec], eval_start: int, last: int) -> SimulationResult:
    run_id = ledger.create_run(
        profile=prof.name, fee_rate=prof.fee_rate, slippage_rate=prof.slippage_rate,
        start_balance=START_BALANCE, instrument=INSTRUMENT, interval=INTERVAL,
        data_source=data.source, data_path=data_path, data_sha256=data.data_sha256,
        bar_count=len(data), warmup_bars=eval_start, eval_bars=last - eval_start + 1,
        eval_start_ms=data.time_ms(eval_start), eval_end_ms=data.time_ms(last),
        seed=SEED, engine_version=__version__,
    )
    journal = JournalAgent(ledger, run_id, data.source)
    risk = RiskAgent()
    execution = PaperExecutionAgent(prof.fee_rate, prof.slippage_rate)

    tests: List[TestResult] = []
    agents: List[StrategyAgent] = []
    books: List[_Book] = []
    for spec in strategies:
        test_id = ledger.create_test(run_id, spec.number, spec.test_name, spec.key,
                                     spec.description, START_BALANCE)
        tests.append(TestResult(test_id, spec.number, spec.test_name, spec.key, START_BALANCE))
        agents.append(StrategyAgent(spec.fn, spec.test_name))
        books.append(_Book(START_BALANCE))

    def apply(res: TestResult, book: _Book, fill: Fill) -> None:
        book.cash += fill.cash_delta
        book.qty += fill.qty_delta
        book.fees += fill.fee
        res.fills.append(fill)
        journal.record_fill(res.test_id, fill, book.cash, book.qty)
        if fill.side == "buy":
            book.entry = fill
            return
        entry = book.entry
        cost = entry.notional
        proceeds = fill.cash_delta
        pnl = proceeds - cost
        trade = {
            "entry_time_ms": entry.fill_time_ms, "entry_price": entry.price,
            "exit_time_ms": fill.fill_time_ms, "exit_price": fill.price,
            "qty": entry.qty, "cost": cost, "proceeds": proceeds,
            "fees": entry.fee + fill.fee, "pnl": pnl,
            "pnl_pct": pnl / cost * HUNDRED if cost else ZERO,
            "exit_reason": fill.reason,
        }
        res.trades.append(trade)
        journal.record_trade(res.test_id, trade)
        book.entry = None

    # One clock for everyone: Data -> Strategy -> Risk -> Execution -> Journal.
    for t, history in data.stream(eval_start - 1, last + 1):
        bar: Bar = history.last
        for res, agent, book in zip(tests, agents, books):
            # Execution of the order decided at t-1 happens at this bar's open.
            if book.pending is not None:
                order, book.pending = book.pending, None
                apply(res, book, execution.fill_next_open(order, bar, t))
            if t == last:
                if book.qty > ZERO:
                    apply(res, book, execution.close_at_end(book.qty, bar, t))
                book.mark(book.cash)
                continue
            signal = agent.decide(history)
            res.signals.append((t, bar.open_time_ms, signal))
            decision = risk.review(signal, book.cash, book.qty, bar.close, t, bar.open_time_ms)
            if decision.approved:
                book.pending = decision.order
            if t >= eval_start:
                book.mark(book.cash + book.qty * bar.close)

    for res, book in zip(tests, books):
        res.final_balance = book.cash
        res.pnl = book.cash - res.start_balance
        res.pnl_pct = res.pnl / res.start_balance * HUNDRED
        res.trade_count = len(res.trades)
        wins = sum(1 for tr in res.trades if tr["pnl"] > ZERO)
        res.win_rate = (Decimal(wins) / Decimal(res.trade_count) * HUNDRED
                        if res.trade_count else None)
        res.max_drawdown_pct = book.max_dd * HUNDRED
        res.fees_paid = book.fees
        journal.finish_test(res.test_id, {
            "final_balance": res.final_balance, "pnl": res.pnl, "pnl_pct": res.pnl_pct,
            "trade_count": res.trade_count, "win_rate": res.win_rate,
            "max_drawdown_pct": res.max_drawdown_pct, "fees_paid": res.fees_paid,
        })

    digest = journal.ledger_hash()
    ledger.finalize_run(run_id, digest)
    return SimulationResult(run_id, digest, prof, data.source, data.time_ms(eval_start),
                            data.time_ms(last), tests)


def run_all(csv_path: str, db_path: str, profile: Union[str, Profile] = "base") -> Tuple[int, str]:
    """Run all ten tests on one CSV at one fee profile. Returns (run_id, ledger_hash)."""
    data = DataAgent.from_csv(csv_path)
    res = simulate(data, profile, db=db_path, data_path=csv_path)
    return res.run_id, res.ledger_hash


def run_bars(bars: Sequence[Bar], profile: Union[str, Profile] = "base",
             source: str = "in-memory", db: Union[str, Ledger, None] = None) -> SimulationResult:
    """Convenience for tests: simulate directly on a list of bars."""
    return simulate(DataAgent(bars, source), profile, db=db)
