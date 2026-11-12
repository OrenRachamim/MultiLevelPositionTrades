"""Plain data types shared by the engine, the brokers and the web layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# --- position states -------------------------------------------------------
ENTERING = "ENTERING"    # entry order is working
INTRADAY = "INTRADAY"    # holding, intraday take-profit is active
SWING = "SWING"          # holding, swing take-profit / stop are active
CLOSED = "CLOSED"        # fully sold
CANCELLED = "CANCELLED"  # entry order never filled
OPEN_STATES = (ENTERING, INTRADAY, SWING)
HOLDING_STATES = (INTRADAY, SWING)

# --- watchlist item states -------------------------------------------------
W_WATCHING = "WATCHING"
W_ENTERED = "ENTERED"
W_NO_SIGNAL = "NO_SIGNAL"
W_SKIPPED = "SKIPPED"

# --- order roles / statuses ------------------------------------------------
ROLE_ENTRY = "ENTRY"
ROLE_TP = "TP"
ROLE_SL = "SL"
ROLE_TRAIL = "TRAIL"     # trailing stop that replaces the stop once the intraday target is reached
PROTECTIVE_ROLES = (ROLE_TP, ROLE_SL, ROLE_TRAIL)
ROLE_EXIT = "EXIT"       # manual / time based exit

O_SUBMITTED = "SUBMITTED"
O_FILLED = "FILLED"
O_CANCELLED = "CANCELLED"
O_REJECTED = "REJECTED"
O_LOST = "LOST"          # broker no longer knows the order
WORKING = (O_SUBMITTED,)
TERMINAL = (O_FILLED, O_CANCELLED, O_REJECTED, O_LOST)

# --- exit reasons ----------------------------------------------------------
R_TP = "TP"
R_SL = "SL"
R_TRAIL = "TRAIL"
R_MANUAL = "MANUAL"
R_TIME = "TIME"
R_EXTERNAL = "EXTERNAL"


@dataclass(slots=True)
class Bar:
    """One-minute bar. ``ts`` is the bar's start, epoch seconds (UTC)."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    rth: bool = True


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: str                      # BUY / SELL
    qty: int
    type: str                      # LMT / STP / MKT / TRAIL
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None     # TRAIL: the stop price the order starts with
    trail_pct: Optional[float] = None      # TRAIL: distance below the highest price, in percent
    tif: str = "DAY"               # DAY / GTC
    outside_rth: bool = False
    oca_group: Optional[str] = None
    ref: str = ""


@dataclass(slots=True)
class OrderUpdate:
    """Cumulative order state as reported by the broker (idempotent)."""

    broker_order_id: str
    status: str                    # one of the O_* statuses
    filled: float = 0.0
    avg_price: float = 0.0
    qty: Optional[float] = None    # total quantity, if the broker changed it
    perm_id: Optional[str] = None
    message: str = ""
    ref: str = ""


@dataclass(slots=True)
class Execution:
    broker_order_id: str
    exec_id: str
    qty: float
    price: float
    ts: int
    commission: Optional[float] = None


@dataclass(slots=True)
class AccountSnapshot:
    net_liq: float                 # USD
    available: float               # USD cash that may be spent right now
    account: str = ""
    as_of: int = 0
    detail: dict = field(default_factory=dict)


def round_price(p: float) -> float:
    """Round to the US equity tick size (1 cent, or 0.01 cent below $1)."""
    return round(p, 2) if p >= 1 else round(p, 4)
