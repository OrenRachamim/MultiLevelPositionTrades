"""US equity market calendar (NYSE / Nasdaq), rule based.

All session logic in the system runs in America/New_York time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc

PRE_OPEN = dt.time(4, 0)      # start of IBKR pre-market
RTH_OPEN = dt.time(9, 30)
RTH_CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)
POST_CLOSE = dt.time(20, 0)   # end of IBKR after-hours
EARLY_POST_CLOSE = dt.time(17, 0)


@dataclass(frozen=True)
class Session:
    date: dt.date
    pre_open: dt.datetime
    open: dt.datetime
    close: dt.datetime
    post_close: dt.datetime

    def is_rth(self, t: dt.datetime) -> bool:
        return self.open <= t < self.close

    def is_extended(self, t: dt.datetime) -> bool:
        """True in pre-market or after-hours (not RTH)."""
        return (self.pre_open <= t < self.open) or (self.close <= t < self.post_close)

    def phase(self, t: dt.datetime) -> str:
        if t < self.pre_open:
            return "CLOSED"
        if t < self.open:
            return "PRE"
        if t < self.close:
            return "RTH"
        if t < self.post_close:
            return "POST"
        return "CLOSED"


def _easter(year: int) -> dt.date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: dt.date) -> dt.date:
    if d.weekday() == 5:      # Saturday -> Friday
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:      # Sunday -> Monday
        return d + dt.timedelta(days=1)
    return d


@lru_cache(maxsize=32)
def holidays(year: int) -> frozenset[dt.date]:
    days = {
        _nth_weekday(year, 1, 0, 3),                    # Martin Luther King Jr.
        _nth_weekday(year, 2, 0, 3),                    # Presidents' Day
        _easter(year) - dt.timedelta(days=2),           # Good Friday
        _last_weekday(year, 5, 0),                      # Memorial Day
        _observed(dt.date(year, 6, 19)),                # Juneteenth
        _observed(dt.date(year, 7, 4)),                 # Independence Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
        _observed(dt.date(year, 12, 25)),               # Christmas
    }
    # New Year's Day: observed on Monday when it falls on Sunday; when it falls
    # on Saturday the market does NOT close on the preceding Friday.
    ny = dt.date(year, 1, 1)
    if ny.weekday() == 6:
        days.add(ny + dt.timedelta(days=1))
    elif ny.weekday() != 5:
        days.add(ny)
    return frozenset(days)


@lru_cache(maxsize=32)
def early_closes(year: int) -> frozenset[dt.date]:
    days = {_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1)}   # day after Thanksgiving
    xmas_eve = dt.date(year, 12, 24)
    if xmas_eve.weekday() < 5:
        days.add(xmas_eve)
    jul3 = dt.date(year, 7, 3)
    if jul3.weekday() < 5 and dt.date(year, 7, 4).weekday() < 5:
        days.add(jul3)
    return frozenset(d for d in days if d not in holidays(year))


class MarketCalendar:
    def __init__(self, extra_holidays: tuple[dt.date, ...] = ()):
        self.extra_holidays = frozenset(extra_holidays)

    def is_trading_day(self, d: dt.date) -> bool:
        return d.weekday() < 5 and d not in holidays(d.year) and d not in self.extra_holidays

    def session(self, d: dt.date) -> Session | None:
        if not self.is_trading_day(d):
            return None
        early = d in early_closes(d.year)

        def at(t: dt.time) -> dt.datetime:
            return dt.datetime.combine(d, t, tzinfo=ET)

        return Session(
            date=d,
            pre_open=at(PRE_OPEN),
            open=at(RTH_OPEN),
            close=at(EARLY_CLOSE if early else RTH_CLOSE),
            post_close=at(EARLY_POST_CLOSE if early else POST_CLOSE),
        )

    def next_trading_day(self, d: dt.date) -> dt.date:
        d += dt.timedelta(days=1)
        while not self.is_trading_day(d):
            d += dt.timedelta(days=1)
        return d

    def trading_days_between(self, start: dt.date, end: dt.date) -> int:
        """Number of trading days after ``start`` up to and including ``end``."""
        n, d = 0, start
        while d < end:
            d += dt.timedelta(days=1)
            if self.is_trading_day(d):
                n += 1
        return n

    def is_rth_ts(self, ts: int) -> bool:
        t = dt.datetime.fromtimestamp(ts, ET)
        s = self.session(t.date())
        return bool(s and s.is_rth(t))


def to_et(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, ET)
