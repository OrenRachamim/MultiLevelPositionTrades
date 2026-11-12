"""Broker interface used by the engine.

Every method is synchronous and non-blocking: orders are *submitted* and their
results come back later through the callbacks. The engine never waits on the
broker, which keeps its logic a plain, testable state machine.
"""

from __future__ import annotations

from typing import Callable, Optional

from ..models import AccountSnapshot, Bar, Execution, OrderRequest, OrderUpdate


def _noop(*_a, **_k) -> None:
    return None


class Broker:
    name = "base"

    def __init__(self) -> None:
        self.connected: bool = False
        # callbacks, wired by the engine
        self.on_bar: Callable[[str, Bar, bool], None] = _noop          # symbol, bar, completed
        self.on_history: Callable[[str, list[Bar]], None] = _noop      # back-fill, completed bars
        self.on_order: Callable[[OrderUpdate], None] = _noop
        self.on_execution: Callable[[Execution], None] = _noop
        self.on_connection: Callable[[bool, str], None] = _noop
        self.on_error: Callable[[str, Optional[str]], None] = _noop    # message, symbol

    # market data ------------------------------------------------------------
    def subscribe(self, symbol: str) -> None:
        raise NotImplementedError

    def unsubscribe(self, symbol: str) -> None:
        raise NotImplementedError

    def ready(self, symbol: str) -> bool:
        """True when orders for ``symbol`` can be placed (contract resolved)."""
        return True

    # account ----------------------------------------------------------------
    def account(self) -> Optional[AccountSnapshot]:
        raise NotImplementedError

    def positions(self) -> Optional[dict[str, float]]:
        """Shares held per symbol, or None when not known yet."""
        raise NotImplementedError

    # orders -----------------------------------------------------------------
    def place(self, req: OrderRequest) -> str:
        """Submit an order and return the broker order id."""
        raise NotImplementedError

    def place_oca(self, reqs: list[OrderRequest]) -> list[str]:
        """Submit a one-cancels-all group (all requests share ``oca_group``)."""
        return [self.place(r) for r in reqs]

    def cancel(self, broker_order_id: str) -> None:
        raise NotImplementedError

    def snapshot_orders(self) -> Optional[list[OrderUpdate]]:
        """State of every order the broker currently knows, None if not synced."""
        raise NotImplementedError
