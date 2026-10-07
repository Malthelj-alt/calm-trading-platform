"""Strategy registry: buy_hold plus the calm strategies from calmbt/calm.py."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import calm


class BuyHold:
    """Buys at the OPEN of the first bar of each window; the engine sells at window end."""

    name = "buy_hold"
    is_placeholder = False
    execution_lag_bars = 0
    initial_target = 1  # engine enters at the first bar's open
    params: Dict[str, Any] = {}

    def reset(self) -> None:
        pass

    def on_bar(self, bar: Any, position: int, history: Optional[Sequence[Any]] = None) -> int:
        return 1

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "class": "BuyHold", "is_placeholder": False,
                "entry": "open of first bar in window", "exit": "close of last bar at or before window end",
                "params": {}}


def _registry() -> Dict[str, Any]:
    reg: Dict[str, Any] = {"buy_hold": BuyHold}
    reg.update(dict(calm.CALM_STRATEGIES))
    return reg


REGISTRY = _registry()


def available() -> List[str]:
    return sorted(REGISTRY)


def make_strategy(name: str):
    try:
        return REGISTRY[name]()
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; known: {available()}")
