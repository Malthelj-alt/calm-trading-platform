"""calmbt — calm paper-trade backtest harness (2025/26, Danish time).

Every strategy is run over the *same* Europe/Copenhagen windows with the same
locked config and the same frozen dataset; only the strategy differs.

Data layer (P1) lives in :mod:`calmbt.data`.
"""
from __future__ import annotations

__version__ = "0.1.0"

DK_TIMEZONE = "Europe/Copenhagen"

__all__ = ["__version__", "DK_TIMEZONE"]
