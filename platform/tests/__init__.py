"""Release-gate tests for calm-paper-trader. Offline: they use data/sample_btcusdt_1h.csv.

Run with:  python3 -m unittest discover -s tests -v
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_CSV = os.path.join(ROOT, "data", "sample_btcusdt_1h.csv")

# `discover -s tests` puts tests/ (not the project root) on sys.path first.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
