"""Intraday entry strategies.

A strategy is a function ``(bars, settings) -> Signal | None`` that is called
once for every completed one-minute bar inside the entry window. ``bars`` ends
with the bar that just closed. To add a strategy, write a function and register
it in ``STRATEGIES``; it then becomes selectable from the dashboard settings.

``macd_cross`` is the system's intraday rule. ``ma_cross`` / ``ma_trend`` are
simple alternatives kept for comparison in replays.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .config import Settings
from .market_calendar import to_et
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


def macd(values: Sequence[float], fast: int, slow: int, signal: int
         ) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """MACD line, signal line and histogram (EMA based, as on TradingView)."""
    ef, es = ema(values, fast), ema(values, slow)
    line: list[Optional[float]] = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    first = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig: list[Optional[float]] = [None] * first + ema(line[first:], signal)   # type: ignore[arg-type]
    hist = [m - g if m is not None and g is not None else None for m, g in zip(line, sig)]
    return line, sig, hist


def macd_cross(bars: Sequence[Bar], s: Settings) -> Optional[Signal]:
    """Buy the first upturn after a sell-off.

    The price falls, the MACD line and its signal line both drop below zero,
    and then the MACD line crosses back up through the signal line while both
    are still below zero. The bar that closes with that cross is the entry.

    "Dropped" is measured on today's bars only: the lowest MACD value of the
    current below-zero stretch must be at least ``macd_min_depth_pct`` percent
    of the share price below zero, and lower than where the MACD line ended
    the previous session - the sell-off has to happen today, a recovery from
    yesterday's sell-off is not a signal.
    """
    if len(bars) < s.macd_slow + s.macd_signal + 1:
        return None
    closes = [b.close for b in bars]
    line, sig, _ = macd(closes, s.macd_fast, s.macd_slow, s.macd_signal)
    m0, m1, g0, g1 = line[-2], line[-1], sig[-2], sig[-1]
    if m0 is None or m1 is None or g0 is None or g1 is None:
        return None
    crossed_up = m0 <= g0 and m1 > g1
    if not crossed_up or m1 >= 0 or g1 >= 0:
        return None
    today = to_et(bars[-1].ts).date()
    low, before_today = m1, None
    for i in range(len(bars) - 1, -1, -1):          # walk back over the below-zero stretch
        v = line[i]
        if to_et(bars[i].ts).date() != today:
            before_today = v
            break
        if v is None or v >= 0:
            break
        low = min(low, v)
    if before_today is not None and low >= before_today:
        return None                                 # the line did not fall today
    depth_pct = -low / closes[-1] * 100
    if depth_pct < s.macd_min_depth_pct:
        return None
    return Signal(closes[-1], f"MACD חצה מעל ה-Signal מתחת לאפס (עומק {depth_pct:.2f}%)")


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
    "macd_cross": macd_cross,
    "ma_cross": ma_cross,
    "ma_trend": ma_trend,
}
