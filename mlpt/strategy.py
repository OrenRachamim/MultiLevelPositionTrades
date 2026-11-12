"""Intraday entry strategies.

A strategy is a function ``(bars, settings) -> Signal | None`` that is called
once for every completed one-minute bar inside the entry window. ``bars`` ends
with the bar that just closed. To add a strategy, write a function and register
it in ``STRATEGIES``; it then becomes selectable from the dashboard settings.

The two strategies below are placeholders until the real intraday rules are
defined - they only show how moving averages are wired in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .config import Settings
from .models import Bar


@dataclass(slots=True)
class Signal:
    price: float     # reference price for the entry (close of the signal bar)
    reason: str


def sma(values: Sequence[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        out.append(total / n if i >= n - 1 else None)
    return out


def ema(values: Sequence[float], n: int) -> list[Optional[float]]:
    """EMA seeded with the SMA of the first ``n`` values."""
    out: list[Optional[float]] = []
    k = 2.0 / (n + 1)
    prev: Optional[float] = None
    total = 0.0
    for i, v in enumerate(values):
        if i < n - 1:
            total += v
            out.append(None)
        elif i == n - 1:
            prev = (total + v) / n
            out.append(prev)
        else:
            prev = v * k + prev * (1 - k)  # type: ignore[operator]
            out.append(prev)
    return out


def moving_average(values: Sequence[float], n: int, kind: str) -> list[Optional[float]]:
    return sma(values, n) if kind == "SMA" else ema(values, n)


def ma_cross(bars: Sequence[Bar], s: Settings) -> Optional[Signal]:
    """Fast MA crosses above the slow MA and the bar closes above both."""
    if len(bars) < s.ma_slow + 1:
        return None
    closes = [b.close for b in bars]
    fast = moving_average(closes, s.ma_fast, s.ma_type)
    slow = moving_average(closes, s.ma_slow, s.ma_type)
    f0, f1, s0, s1 = fast[-2], fast[-1], slow[-2], slow[-1]
    if None in (f0, f1, s0, s1):
        return None
    if f0 <= s0 and f1 > s1 and closes[-1] > f1:  # type: ignore[operator]
        return Signal(closes[-1], f"{s.ma_type}{s.ma_fast} חצה מעל {s.ma_type}{s.ma_slow}")
    return None


def ma_trend(bars: Sequence[Bar], s: Settings) -> Optional[Signal]:
    """Up-trend: close > fast MA > slow MA on a rising bar (no cross needed)."""
    if len(bars) < s.ma_slow + 1:
        return None
    closes = [b.close for b in bars]
    f1 = moving_average(closes, s.ma_fast, s.ma_type)[-1]
    s1 = moving_average(closes, s.ma_slow, s.ma_type)[-1]
    if f1 is None or s1 is None:
        return None
    if closes[-1] > f1 > s1 and closes[-1] > bars[-1].open:
        return Signal(closes[-1], f"מחיר > {s.ma_type}{s.ma_fast} > {s.ma_type}{s.ma_slow}")
    return None


STRATEGIES: dict[str, Callable[[Sequence[Bar], Settings], Optional[Signal]]] = {
    "ma_cross": ma_cross,
    "ma_trend": ma_trend,
}
