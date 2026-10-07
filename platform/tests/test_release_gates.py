"""Release gates (a)-(d):

(a) replaying a run gives an identical ledger sha256,
(b) a poisoned future cannot change decisions already made,
(c) every fill is priced from the open of the bar after its signal bar (t+1),
(d) Buy & Hold 01 matches the hand formula within 1e-9 at both fee profiles.

All tests are offline and run against data/sample_btcusdt_1h.csv.
"""
import decimal
import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import SAMPLE_CSV  # noqa: E402
from tradebot.agents import Bar, DataAgent, Order, PaperExecutionAgent, load_bars  # noqa: E402
from tradebot.engine import PROFILES, START_BALANCE, run_all, simulate  # noqa: E402
from tradebot.ledger import compute_ledger_hash, connect, ledger_hash  # noqa: E402
from tradebot.strategies import STRATEGIES  # noqa: E402

TOL = Decimal("1e-9")


def _fills_from_db(db_path, run_id):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT f.*, t.name AS test_name FROM fills f JOIN tests t ON t.id = f.test_id"
            " WHERE f.run_id = ? ORDER BY t.number, f.seq", (run_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class _TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="calm_trader_test_")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def db(self, name):
        return os.path.join(self.tmp, name)


# =============================================================== (a) replay determinism
class TestReplayDeterminism(_TempDirCase):
    def test_two_runs_into_separate_dbs_give_identical_hash(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                run_a, hash_a = run_all(SAMPLE_CSV, self.db("a_%s.db" % profile), profile)
                run_b, hash_b = run_all(SAMPLE_CSV, self.db("b_%s.db" % profile), profile)
                self.assertRegex(hash_a, r"^[0-9a-f]{64}$")
                self.assertEqual(hash_a, hash_b)

    def test_stored_hash_matches_recomputed_hash(self):
        db = self.db("stored.db")
        run_id, digest = run_all(SAMPLE_CSV, db, "base")
        self.assertEqual(ledger_hash(db, run_id), digest)
        conn = sqlite3.connect(db)
        try:
            stored, status = conn.execute(
                "SELECT ledger_hash, status FROM runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(stored, digest)
        self.assertEqual(status, "complete")

    def test_replay_into_db_with_earlier_runs_gives_same_hash(self):
        # Row ids differ (run 1 vs run 3); the canonical hash must not depend on them.
        fresh_id, fresh_hash = run_all(SAMPLE_CSV, self.db("fresh.db"), "base")
        shared = self.db("shared.db")
        run_all(SAMPLE_CSV, shared, "base")
        run_all(SAMPLE_CSV, shared, "stress")
        later_id, later_hash = run_all(SAMPLE_CSV, shared, "base")
        self.assertNotEqual(fresh_id, later_id)
        self.assertEqual(fresh_hash, later_hash)

    def test_fee_profiles_hash_differently(self):
        _, base = run_all(SAMPLE_CSV, self.db("p.db"), "base")
        _, stress = run_all(SAMPLE_CSV, self.db("p.db"), "stress")
        self.assertNotEqual(base, stress)

    def test_any_change_to_a_fill_changes_the_hash(self):
        # The hash is a real fingerprint: tampering (bypassing the triggers on a
        # copy) must be detected.
        db = self.db("tamper.db")
        run_id, digest = run_all(SAMPLE_CSV, db, "base")
        conn = sqlite3.connect(db)
        try:
            conn.execute("DROP TRIGGER fills_no_update")
            conn.execute("UPDATE fills SET price = price || '1' WHERE id = "
                         "(SELECT MIN(id) FROM fills WHERE run_id = ?)", (run_id,))
            conn.commit()
            self.assertNotEqual(compute_ledger_hash(conn, run_id), digest)
        finally:
            conn.close()

    def test_ledger_is_append_only(self):
        db = self.db("append_only.db")
        run_id, _ = run_all(SAMPLE_CSV, db, "base")
        conn = sqlite3.connect(db)
        try:
            for sql in ("UPDATE fills SET price = '1'", "DELETE FROM fills",
                        "UPDATE trades SET pnl = '1'", "DELETE FROM trades",
                        "UPDATE tests SET final_balance = '1000000'", "DELETE FROM tests",
                        "UPDATE runs SET ledger_hash = 'x'", "DELETE FROM runs"):
                with self.subTest(sql=sql):
                    with self.assertRaises(sqlite3.DatabaseError):
                        conn.execute(sql)
        finally:
            conn.close()

    def test_ledger_records_data_source(self):
        db = self.db("source.db")
        run_id, _ = run_all(SAMPLE_CSV, db, "base")
        conn = sqlite3.connect(db)
        try:
            (run_source,) = conn.execute(
                "SELECT data_source FROM runs WHERE id = ?", (run_id,)).fetchone()
            fill_sources = {r[0] for r in conn.execute(
                "SELECT DISTINCT data_source FROM fills WHERE run_id = ?", (run_id,))}
        finally:
            conn.close()
        self.assertIn("sample", run_source.lower())
        self.assertEqual(fill_sources, {run_source})


# =============================================================== (b) poisoned future
class TestPoisonedFuture(unittest.TestCase):
    """Replace every bar after a cut point t with garbage. Every signal decided at or
    before t, every order decided at or before t, and every fill executed at or
    before t must be exactly the same as on the clean data."""

    CUTS = (230, 400, 640, 870)

    @classmethod
    def setUpClass(cls):
        cls.bars = load_bars(SAMPLE_CSV)
        cls.n = len(cls.bars)
        cls.clean = simulate(DataAgent(cls.bars, "clean"), "base")

    @staticmethod
    def _poison(bars, cut, value):
        out = list(bars[:cut + 1])
        for b in bars[cut + 1:]:
            # Keep the timestamps (the clock is shared); poison every price/volume.
            out.append(Bar(b.open_time_ms, value, value, value, value, value))
        return out

    def _check(self, poisoned_result, cut):
        for clean_t, dirty_t in zip(self.clean.tests, poisoned_result.tests):
            self.assertEqual(clean_t.name, dirty_t.name)
            with self.subTest(test=clean_t.name, cut=cut):
                # Signals decided on bars 0..cut.
                clean_sig = [s for s in clean_t.signals if s[0] <= cut]
                dirty_sig = [s for s in dirty_t.signals if s[0] <= cut]
                self.assertTrue(clean_sig, "no signals before the cut; cut is too early")
                self.assertEqual(clean_sig, dirty_sig)
                # Fills executed on bars 0..cut are identical in every field.
                clean_fills = [f for f in clean_t.fills if f.fill_index <= cut]
                dirty_fills = [f for f in dirty_t.fills if f.fill_index <= cut]
                self.assertEqual(clean_fills, dirty_fills)
                # Orders decided on bar <= cut (incl. the one at cut that fills at
                # cut+1 on poisoned prices) keep their side and decision bar.
                clean_orders = [(f.side, f.signal_index) for f in clean_t.fills
                                if f.reason == "signal" and f.signal_index <= cut]
                dirty_orders = [(f.side, f.signal_index) for f in dirty_t.fills
                                if f.reason == "signal" and f.signal_index <= cut]
                self.assertEqual(clean_orders, dirty_orders)

    def test_nan_future(self):
        nan = Decimal("NaN")
        for cut in self.CUTS:
            res = simulate(DataAgent(self._poison(self.bars, cut, nan), "poisoned-nan"), "base")
            self._check(res, cut)

    def test_huge_future(self):
        huge = Decimal("1e12")
        for cut in self.CUTS:
            res = simulate(DataAgent(self._poison(self.bars, cut, huge), "poisoned-1e12"), "base")
            self._check(res, cut)

    def test_strategies_never_read_past_the_decision_bar(self):
        # Strategy level, every bar: a strategy must answer the same on the clean
        # history and on a history whose (unreachable) future is poisoned.
        cut = 500
        clean = DataAgent(self.bars, "clean")
        dirty = DataAgent(self._poison(self.bars, cut, Decimal("1e12")), "dirty")
        for spec in STRATEGIES:
            with self.subTest(strategy=spec.test_name):
                for t in range(cut - 40, cut + 1):
                    self.assertEqual(spec.fn(clean.history(t)), spec.fn(dirty.history(t)))
                    self.assertEqual(len(dirty.history(t)), t + 1)


# =============================================================== (c) fills at t+1
class TestFillsAtNextOpen(_TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = load_bars(SAMPLE_CSV)
        cls.n = len(cls.bars)

    def _assert_fill_rows(self, rows, profile):
        s = PROFILES[profile].slippage_rate
        last = self.n - 1
        eval_start = max(200, self.n - 720)
        self.assertTrue(rows, "no fills at all")
        signal_fills = 0
        for r in rows:
            with self.subTest(test=r["test_name"], seq=r["seq"]):
                si, fi = int(r["signal_index"]), int(r["fill_index"])
                ref, price = Decimal(r["ref_price"]), Decimal(r["price"])
                self.assertEqual(int(r["signal_time_ms"]), self.bars[si].open_time_ms)
                self.assertEqual(int(r["fill_time_ms"]), self.bars[fi].open_time_ms)
                self.assertGreaterEqual(fi, eval_start, "fill inside the warm-up")
                if r["reason"] == "signal":
                    signal_fills += 1
                    self.assertEqual(fi, si + 1, "fill must be at t+1")
                    self.assertEqual(ref, self.bars[si + 1].open)
                    factor = (1 + s) if r["side"] == "buy" else (1 - s)
                    self.assertLessEqual(abs(price - self.bars[si + 1].open * factor), TOL)
                else:
                    # Only the forced close of an open position on the very last bar.
                    self.assertEqual(r["reason"], "final_close")
                    self.assertEqual(r["side"], "sell")
                    self.assertEqual(fi, last)
                    self.assertEqual(ref, self.bars[last].close)
                    self.assertLessEqual(abs(price - self.bars[last].close * (1 - s)), TOL)
        self.assertGreater(signal_fills, 0)

    def test_every_ledger_fill_is_priced_from_next_open(self):
        for profile in PROFILES:
            with self.subTest(profile=profile):
                db = self.db("fills_%s.db" % profile)
                run_id, _ = run_all(SAMPLE_CSV, db, profile)
                self._assert_fill_rows(_fills_from_db(db, run_id), profile)

    def test_in_memory_fills_match_t_plus_one(self):
        res = simulate(DataAgent(self.bars, "mem"), "base")
        for t in res.tests:
            for f in t.fills:
                if f.reason == "signal":
                    self.assertEqual(f.fill_index, f.signal_index + 1, t.name)
                    self.assertEqual(f.ref_price, self.bars[f.fill_index].open, t.name)

    def test_no_signal_fill_ever_uses_the_signal_bar_close(self):
        # A same-bar fill (look-ahead) would use close[t]; t+1 open must be used.
        db = self.db("sameb.db")
        run_id, _ = run_all(SAMPLE_CSV, db, "base")
        for r in _fills_from_db(db, run_id):
            if r["reason"] == "signal":
                self.assertNotEqual(int(r["fill_index"]), int(r["signal_index"]))

    def test_execution_agent_refuses_fills_other_than_t_plus_one(self):
        ex = PaperExecutionAgent(Decimal("0.001"), Decimal("0.0005"))
        order = Order("buy", Decimal("50"), Decimal("0"), 300, self.bars[300].open_time_ms)
        for bad in (300, 302, 299):
            with self.subTest(fill_index=bad):
                with self.assertRaises(ValueError):
                    ex.fill_next_open(order, self.bars[bad], bad)
        fill = ex.fill_next_open(order, self.bars[301], 301)
        self.assertEqual(fill.ref_price, self.bars[301].open)


# =============================================================== (d) buy & hold formula
class TestBuyAndHoldFormula(_TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = load_bars(SAMPLE_CSV)
        n = len(cls.bars)
        cls.eval_start = max(200, n - 720)
        cls.p_open = cls.bars[cls.eval_start].open
        cls.p_end = cls.bars[n - 1].close

    def hand(self, f, s):
        with decimal.localcontext() as ctx:
            ctx.prec = 60
            qty = Decimal(50) * (1 - f) / (self.p_open * (1 + s))
            return qty, qty * self.p_end * (1 - f) * (1 - s)

    def test_final_value_matches_hand_formula_both_profiles(self):
        for name, prof in PROFILES.items():
            with self.subTest(profile=name):
                db = self.db("bh_%s.db" % name)
                run_id, _ = run_all(SAMPLE_CSV, db, name)
                exp_qty, expected = self.hand(prof.fee_rate, prof.slippage_rate)
                conn = connect(db)
                try:
                    test = conn.execute(
                        "SELECT * FROM tests WHERE run_id = ? AND name = 'Buy & Hold 01'",
                        (run_id,)).fetchone()
                    fills = conn.execute(
                        "SELECT * FROM fills WHERE test_id = ? ORDER BY seq",
                        (test["id"],)).fetchall()
                finally:
                    conn.close()
                final = Decimal(test["final_balance"])
                self.assertLessEqual(abs(final - expected), TOL,
                                     "final %s vs hand %s" % (final, expected))
                self.assertEqual(int(test["trade_count"]), 1)
                self.assertEqual([f["side"] for f in fills], ["buy", "sell"])
                self.assertEqual(int(fills[0]["fill_index"]), self.eval_start)
                self.assertEqual(Decimal(fills[0]["ref_price"]), self.p_open)
                self.assertLessEqual(abs(Decimal(fills[0]["qty"]) - exp_qty), TOL)
                self.assertEqual(fills[1]["reason"], "final_close")
                self.assertEqual(Decimal(fills[1]["ref_price"]), self.p_end)
                pnl = Decimal(test["pnl"])
                self.assertLessEqual(abs(pnl - (expected - START_BALANCE)), TOL)

    def test_stress_fee_costs_more_than_base(self):
        _, base = self.hand(PROFILES["base"].fee_rate, PROFILES["base"].slippage_rate)
        _, stress = self.hand(PROFILES["stress"].fee_rate, PROFILES["stress"].slippage_rate)
        res_b = simulate(DataAgent(self.bars, "m"), "base").by_name("Buy & Hold 01")
        res_s = simulate(DataAgent(self.bars, "m"), "stress").by_name("Buy & Hold 01")
        self.assertLess(res_s.final_balance, res_b.final_balance)
        self.assertLessEqual(abs(res_b.final_balance - base), TOL)
        self.assertLessEqual(abs(res_s.final_balance - stress), TOL)


if __name__ == "__main__":
    unittest.main()
