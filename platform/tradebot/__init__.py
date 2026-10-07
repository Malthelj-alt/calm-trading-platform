"""calm-paper-trader: a deterministic, simulated-only BTC/USDT paper-trading engine.

Simulated — no real money. This package contains no order-sending, broker or
authenticated exchange code; every fill is computed from historical bars.

Public API:
    run_all(csv_path, db_path, profile="base") -> (run_id, ledger_hash)
    simulate(DataAgent, profile, db=None)      -> SimulationResult (in-memory detail)
    STRATEGIES / TEST_NAMES                    -> the ten tests, "Buy & Hold 01" .. "Random Control 10"
    PROFILES                                   -> {"base": f=0.001 s=0.0005, "stress": f=0.004 s=0.0005}
"""
__version__ = "1.0.0"

from .engine import (EVAL_BARS, PROFILES, START_BALANCE, WARMUP_BARS, Profile,  # noqa: E402
                     SimulationResult, TestResult, get_profile, run_all, run_bars, simulate)
from .strategies import STRATEGIES, TEST_NAMES  # noqa: E402

__all__ = [
    "__version__", "run_all", "run_bars", "simulate", "get_profile", "Profile", "PROFILES",
    "SimulationResult", "TestResult", "STRATEGIES", "TEST_NAMES", "START_BALANCE",
    "WARMUP_BARS", "EVAL_BARS",
]
