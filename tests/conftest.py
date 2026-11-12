from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlpt import strategy  # noqa: E402
from mlpt.broker.sim import SimBroker  # noqa: E402
from mlpt.config import Settings  # noqa: E402
from mlpt.db import Store  # noqa: E402
from mlpt.engine import Engine  # noqa: E402
from mlpt.market_calendar import ET, MarketCalendar  # noqa: E402
from mlpt.models import Bar  # noqa: E402
from mlpt.notifier import Notifier  # noqa: E402
from mlpt.strategy import Rejected, Signal  # noqa: E402

D1 = dt.date(2026, 11, 10)   # Tuesday
D2 = dt.date(2026, 11, 11)   # Wednesday
D3 = dt.date(2026, 11, 12)


def _flag(bars, settings):
    """Test strategy: volume 777 is an entry signal, volume 555 a trigger that a rule rejects."""
    if bars and bars[-1].volume == 777:
        return Signal(bars[-1].close, "test")
    if bars and bars[-1].volume == 555:
        return Rejected("test rule")
    return None


strategy.STRATEGIES["flag"] = _flag


def ts(day: dt.date, hh: int, mm: int = 0) -> int:
    return int(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).timestamp())


class Rig:
    """Engine + simulated broker, stepped one minute at a time."""

    def __init__(self, cash: float = 100_000.0, store: Store | None = None, **settings):
        self.cal = MarketCalendar()
        self.store = store or Store(":memory:")
        base = dict(strategy="flag", position_pct=10.0, intraday_tp_pct=0.5, swing_tp_pct=5.0,
                    swing_sl_pct=4.0, entry_slippage_pct=0.1)
        self.store.save_settings(Settings.from_dict({**base, **settings}))
        self.sim = SimBroker(self.cal, cash=cash, start_ts=ts(D1, 9, 0), commissions=False)
        self.notifier = Notifier()
        self.engine = Engine(self.store, self.sim, self.cal, self.notifier, self.sim.now, "sim")
        self.engine.tick()

    def restart(self) -> None:
        """A new engine process on the same database and the same broker."""
        self.notifier = Notifier()
        self.engine = Engine(self.store, self.sim, self.cal, self.notifier, self.sim.now, "sim")
        self.engine.tick()

    def load(self, text: str, day: dt.date = D1) -> dict:
        res = self.engine.load_watchlist(day.isoformat(), text)
        assert res["ok"], res
        self.engine.tick()
        return res

    def step(self, t: int, sym: str | None = None, price: float | None = None, *, high: float | None = None,
             low: float | None = None, open_: float | None = None, signal: bool = False) -> None:
        bars = {}
        if sym is not None:
            o = self.sim.last.get(sym, price) if open_ is None else open_   # opens at the previous close
            bars[sym] = Bar(t, o, max(o, price, high or price), min(o, price, low or price), price,
                            777 if signal else 100)
        self.sim.feed(t, bars)
        self.engine.tick()

    def run(self, sym: str, day: dt.date, start: tuple[int, int], prices: list[float]) -> int:
        """Feed consecutive flat bars; returns the ts after the last one."""
        t = ts(day, *start)
        for p in prices:
            self.step(t, sym, p)
            t += 60
        return t

    def positions(self, *states: str) -> list[dict]:
        rows = self.store._all("SELECT * FROM positions ORDER BY id")
        return [r for r in rows if not states or r["state"] in states]

    def working(self) -> list[dict]:
        return self.store.orders_working()

    def events(self, kind: str = "") -> list[str]:
        return [e["message"] for e in self.store.events(500) if not kind or e["kind"] == kind]


@pytest.fixture
def rig() -> Rig:
    return Rig()
