"""Release gates (e) and (f), plus the "no real order path" guard.

(e) exactly 10 tests named '<strategy> NN', each starting at $50, no negative balance;
(f) a notional below $5 is rejected by the RiskAgent.
"""
import os
import re
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import ROOT, SAMPLE_CSV  # noqa: E402
import tradebot.engine as engine  # noqa: E402
from tradebot.agents import DataAgent, RiskAgent, load_bars  # noqa: E402
from tradebot.engine import PROFILES, run_all, simulate  # noqa: E402
from tradebot.ledger import connect  # noqa: E402
from tradebot.strategies import STRATEGIES, TEST_NAMES  # noqa: E402

EXPECTED_NAMES = [
    "Buy & Hold 01", "SMA Cross 20/50 02", "SMA Cross 50/200 03", "EMA Cross 12/26 04",
    "RSI Reversion 05", "Bollinger Reversion 06", "Donchian Breakout 07", "Momentum 08",
    "MACD 09", "Random Control 10",
]
NAME_RE = re.compile(r"^(?P<strategy>\S.*\S) (?P<nn>\d{2})$")


# =============================================================== (e) names, $50, no negatives
class TestTenNamedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="calm_trader_names_")
        cls.db = os.path.join(cls._tmp.name, "names.db")
        cls.runs = {p: run_all(SAMPLE_CSV, cls.db, p)[0] for p in PROFILES}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _rows(self, sql, params):
        conn = connect(self.db)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def test_registry_has_exactly_ten_named_tests(self):
        self.assertEqual(len(STRATEGIES), 10)
        self.assertEqual(TEST_NAMES, EXPECTED_NAMES)
        for i, spec in enumerate(STRATEGIES, start=1):
            m = NAME_RE.match(spec.test_name)
            self.assertIsNotNone(m, spec.test_name)
            self.assertEqual(m.group("strategy"), spec.name)
            self.assertEqual(int(m.group("nn")), i)
            self.assertEqual(spec.number, i)

    def test_both_profiles_store_ten_tests_starting_at_50(self):
        self.assertEqual(sorted(self.runs), ["base", "stress"])
        for profile, run_id in self.runs.items():
            with self.subTest(profile=profile):
                tests = self._rows("SELECT * FROM tests WHERE run_id = ? ORDER BY number",
                                   (run_id,))
                self.assertEqual([t["name"] for t in tests], EXPECTED_NAMES)
                self.assertEqual([t["number"] for t in tests], list(range(1, 11)))
                for t in tests:
                    self.assertEqual(Decimal(t["start_balance"]), Decimal("50"), t["name"])
                    self.assertIsNotNone(t["final_balance"], t["name"])
                run = self._rows("SELECT * FROM runs WHERE id = ?", (run_id,))[0]
                self.assertEqual(Decimal(run["start_balance"]), Decimal("50"))

    def test_same_clock_and_costs_for_every_test(self):
        # Only the strategy may differ: every run shares bars, window and costs.
        for profile, run_id in self.runs.items():
            run = self._rows("SELECT * FROM runs WHERE id = ?", (run_id,))[0]
            self.assertEqual(Decimal(run["fee_rate"]), PROFILES[profile].fee_rate)
            self.assertEqual(Decimal(run["slippage_rate"]), Decimal("0.0005"))
            self.assertGreaterEqual(int(run["warmup_bars"]), 200)
        self.assertEqual(PROFILES["base"].fee_rate, Decimal("0.001"))
        self.assertEqual(PROFILES["stress"].fee_rate, Decimal("0.004"))

    def test_no_balance_or_position_ever_goes_negative(self):
        for profile, run_id in self.runs.items():
            with self.subTest(profile=profile):
                fills = self._rows("SELECT * FROM fills WHERE run_id = ?", (run_id,))
                self.assertTrue(fills)
                for f in fills:
                    self.assertGreaterEqual(Decimal(f["cash_after"]), 0, f)
                    self.assertGreaterEqual(Decimal(f["position_after"]), 0, f)
                    self.assertGreater(Decimal(f["qty"]), 0, f)
                    self.assertGreaterEqual(Decimal(f["fee"]), 0, f)
                for t in self._rows("SELECT * FROM tests WHERE run_id = ?", (run_id,)):
                    self.assertGreaterEqual(Decimal(t["final_balance"]), 0, t["name"])
                    self.assertEqual(Decimal(t["final_balance"]) - Decimal(t["start_balance"]),
                                     Decimal(t["pnl"]), t["name"])

    def test_every_test_ends_flat(self):
        # Long only, closed at the last bar: the final fill of each test leaves no position.
        for run_id in self.runs.values():
            tests = self._rows("SELECT id, name FROM tests WHERE run_id = ?", (run_id,))
            for t in tests:
                fills = self._rows("SELECT * FROM fills WHERE test_id = ? ORDER BY seq",
                                   (t["id"],))
                if fills:
                    self.assertEqual(Decimal(fills[-1]["position_after"]), 0, t["name"])
                    sides = [f["side"] for f in fills]
                    # Strictly alternating buy/sell starting with buy: no shorting, no pyramiding.
                    self.assertEqual(sides, ["buy", "sell"] * (len(sides) // 2), t["name"])

    def test_random_control_is_seeded(self):
        bars = load_bars(SAMPLE_CSV)
        a = simulate(DataAgent(bars, "m"), "base").by_name("Random Control 10")
        b = simulate(DataAgent(bars, "m"), "base").by_name("Random Control 10")
        self.assertEqual(a.signals, b.signals)
        self.assertEqual(a.final_balance, b.final_balance)
        self.assertEqual({s[2] for s in a.signals}, {"long", "flat"})


# =============================================================== (f) $5 minimum notional
class TestRiskAgentMinimumNotional(unittest.TestCase):
    def setUp(self):
        self.risk = RiskAgent()

    def test_minimum_is_five_dollars(self):
        self.assertEqual(RiskAgent.MIN_NOTIONAL, Decimal("5"))

    def test_buy_below_five_is_rejected(self):
        for cash in ("4.99", "4.999999999", "0.01", "0"):
            with self.subTest(cash=cash):
                d = self.risk.review("long", Decimal(cash), Decimal("0"), Decimal("60000"))
                self.assertFalse(d.approved)
                self.assertIsNone(d.order)
                self.assertEqual(d.reason, "below_min_notional")

    def test_buy_at_or_above_five_is_approved_with_full_sizing(self):
        for cash in ("5", "5.01", "50"):
            with self.subTest(cash=cash):
                d = self.risk.review("long", Decimal(cash), Decimal("0"), Decimal("60000"), 300, 1)
                self.assertTrue(d.approved)
                self.assertEqual(d.order.side, "buy")
                self.assertEqual(d.order.notional, Decimal(cash))  # 100 %, never more
                self.assertEqual(d.order.signal_index, 300)

    def test_sell_below_five_is_rejected(self):
        price = Decimal("60000")
        qty = Decimal("4.99") / price
        d = self.risk.review("flat", Decimal("0"), qty, price)
        self.assertFalse(d.approved)
        self.assertEqual(d.reason, "below_min_notional")
        ok = self.risk.review("flat", Decimal("0"), Decimal("5") / price, price)
        self.assertTrue(ok.approved)
        self.assertEqual(ok.order.side, "sell")
        self.assertEqual(ok.order.qty, Decimal("5") / price)

    def test_long_only_and_no_pyramiding(self):
        # Flat with no position: nothing to sell, never a short.
        d = self.risk.review("flat", Decimal("50"), Decimal("0"), Decimal("60000"))
        self.assertFalse(d.approved)
        self.assertEqual(d.reason, "hold")
        # Long while already long: no second buy.
        d = self.risk.review("long", Decimal("50"), Decimal("0.001"), Decimal("60000"))
        self.assertFalse(d.approved)
        self.assertEqual(d.reason, "hold")

    def test_negative_state_is_refused(self):
        d = self.risk.review("long", Decimal("-1"), Decimal("0"), Decimal("60000"))
        self.assertFalse(d.approved)
        d = self.risk.review("flat", Decimal("50"), Decimal("-0.1"), Decimal("60000"))
        self.assertFalse(d.approved)

    def test_unknown_signal_raises(self):
        with self.assertRaises(ValueError):
            self.risk.review("short", Decimal("50"), Decimal("0"), Decimal("60000"))

    def test_engine_never_trades_an_account_below_five(self):
        # End to end: with a $4.99 account every buy is refused, so no fills at all.
        bars = load_bars(SAMPLE_CSV)
        with mock.patch.object(engine, "START_BALANCE", Decimal("4.99")):
            res = simulate(DataAgent(bars, "tiny"), "base")
        for t in res.tests:
            with self.subTest(test=t.name):
                self.assertEqual(t.fills, [])
                self.assertEqual(t.final_balance, Decimal("4.99"))


# =============================================================== no real order path
class TestNoRealOrderPath(unittest.TestCase):
    FORBIDDEN = (
        r"urllib\.request", r"http\.client", r"\bimport\s+socket\b", r"\bimport\s+requests\b",
        r"\bimport\s+ssl\b", r"\bhmac\b", r"X-MBX-APIKEY", r"/api/v3/order", r"api_secret",
        r"\burlopen\b",
    )

    def test_engine_package_has_no_network_or_order_code(self):
        pkg = os.path.join(ROOT, "tradebot")
        files = sorted(f for f in os.listdir(pkg) if f.endswith(".py"))
        self.assertIn("agents.py", files)
        for name in files:
            with open(os.path.join(pkg, name), encoding="utf-8") as fh:
                src = fh.read()
            for pat in self.FORBIDDEN:
                with self.subTest(file=name, pattern=pat):
                    self.assertIsNone(re.search(pat, src), "%s matches %s" % (name, pat))

    def test_fetch_script_never_places_orders(self):
        path = os.path.join(ROOT, "scripts", "fetch_binance.py")
        if not os.path.exists(path):
            self.skipTest("fetch script not present")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for pat in (r"/api/v3/order", r"X-MBX-APIKEY", r"\bhmac\b", r"signature=",
                    r"method\s*=\s*['\"](POST|DELETE|PUT)"):
            with self.subTest(pattern=pat):
                self.assertIsNone(re.search(pat, src))


if __name__ == "__main__":
    unittest.main()
