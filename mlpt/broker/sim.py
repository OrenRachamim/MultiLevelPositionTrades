"""Simulated broker: fills orders against one-minute bars.

Used by the tests, by ``demo`` (synthetic prices) and by ``replay`` (recorded
bars). Simulated time is driven by ``feed`` / ``advance`` - nothing here looks
at the wall clock.

Fill model (deliberately conservative):
* an order can only fill on bars that start after it was placed;
* buy limit  - fills when the bar trades at or below the limit;
* sell limit - fills when the bar trades at or above the limit;
* sell stop  - fills when the bar trades at or below the stop, at the stop
  price or at the open when the bar gaps below it;
* when a bar touches both the stop and the take-profit, the stop wins;
* trailing stop - checked against the stop it carried into the bar (gap: the
  open), then raised to the bar's high minus the trail; it also fills at the
  raised stop when the bar closes at or below it. With one-minute bars and a
  trail of a few tenths of a percent this is only an approximation;
* orders without ``outside_rth`` only work on regular-hours bars.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass
from typing import Iterator, Optional

from ..market_calendar import ET, MarketCalendar, to_et
from ..models import (O_CANCELLED, O_FILLED, O_REJECTED, O_SUBMITTED, AccountSnapshot, Bar,
                      Execution, OrderRequest, OrderUpdate)
from .base import Broker


@dataclass
class _SimOrder:
    id: str
    req: OrderRequest
    placed_at: float
    status: str = O_SUBMITTED
    filled: float = 0.0
    avg_price: float = 0.0
    stop: float = 0.0            # TRAIL: where the trailing stop stands now


class SimBroker(Broker):
    name = "sim"

    def __init__(self, calendar: MarketCalendar, cash: float = 100_000.0,
                 start_ts: float = 0.0, commissions: bool = True):
        super().__init__()
        self.calendar = calendar
        self.cash = cash
        self.commissions = commissions
        self.time: float = start_ts
        self.connected = True
        self.history: dict[str, list[Bar]] = {}      # delivered on subscribe
        self.subscribed: set[str] = set()
        self.last: dict[str, float] = {}
        self.pos: dict[str, float] = {}
        self.orders: dict[str, _SimOrder] = {}
        self.unsettled: list[tuple[dt.date, float]] = []
        self._seq = 0
        self._exec_seq = 0
        self._queue: list[tuple[str, object]] = []

    # --- clock ---------------------------------------------------------------
    def now(self) -> float:
        return self.time

    # --- Broker API ----------------------------------------------------------
    def subscribe(self, symbol: str) -> None:
        self.subscribed.add(symbol)
        hist = [b for b in self.history.get(symbol, []) if b.ts + 60 <= self.time]
        if hist:
            self.on_history(symbol, hist)

    def unsubscribe(self, symbol: str) -> None:
        self.subscribed.discard(symbol)

    def account(self) -> Optional[AccountSnapshot]:
        today = to_et(self.time).date()
        pending = sum(a for d, a in self.unsettled if d > today)
        net = self.cash + sum(q * self.last.get(s, 0.0) for s, q in self.pos.items())
        return AccountSnapshot(net_liq=net, available=self.cash - pending, account="SIM",
                               as_of=int(self.time))

    def positions(self) -> Optional[dict[str, float]]:
        return {s: q for s, q in self.pos.items() if q}

    def place(self, req: OrderRequest) -> str:
        self._seq += 1
        o = _SimOrder(id=f"S{self._seq}", req=req, placed_at=self.time, stop=req.stop_price or 0.0)
        self.orders[o.id] = o
        self._emit(o)
        return o.id

    def cancel(self, broker_order_id: str) -> None:
        o = self.orders.get(broker_order_id)
        if o and o.status == O_SUBMITTED:
            o.status = O_CANCELLED
            self._emit(o)

    def snapshot_orders(self) -> Optional[list[OrderUpdate]]:
        return [self._update(o) for o in self.orders.values()]

    # --- simulation drive ----------------------------------------------------
    def feed(self, ts: int, bars: dict[str, Bar]) -> None:
        """Process the one-minute bars that start at ``ts`` and deliver events."""
        self.flush()                       # acks for orders placed since the last step
        self.advance(ts)
        for symbol, bar in bars.items():
            bar.rth = self.calendar.is_rth_ts(bar.ts)
            self._match(symbol, bar)
            self.last[symbol] = bar.close
            # like a real broker's back-fill: a stock subscribed later still gets the recent days
            hist = self.history.setdefault(symbol, [])
            if not hist or bar.ts > hist[-1].ts:
                hist.append(bar)
                if len(hist) > 4000:
                    del hist[:1000]
        self.time = ts + 60
        self.flush()
        for symbol, bar in bars.items():
            if symbol in self.subscribed:
                self.on_bar(symbol, bar, True)
        self.flush()

    def advance(self, ts: float) -> None:
        """Move the clock without bars: expires DAY orders, settles cash."""
        if ts > self.time:
            self.time = ts
        now_et = to_et(self.time)
        for o in list(self.orders.values()):
            if o.status != O_SUBMITTED or o.req.tif != "DAY":
                continue
            placed = to_et(o.placed_at)
            sess = self.calendar.session(placed.date())
            if sess is None:
                continue
            expiry = sess.post_close if o.req.outside_rth else sess.close
            if placed < expiry <= now_et:
                o.status = O_CANCELLED
                self._emit(o, "DAY order expired")
        self.unsettled = [(d, a) for d, a in self.unsettled if d > now_et.date()]
        self.flush()

    def flush(self) -> None:
        while self._queue:
            kind, payload = self._queue.pop(0)
            if kind == "order":
                self.on_order(payload)       # type: ignore[arg-type]
            else:
                self.on_execution(payload)   # type: ignore[arg-type]

    # --- internals -----------------------------------------------------------
    def _update(self, o: _SimOrder, message: str = "") -> OrderUpdate:
        return OrderUpdate(broker_order_id=o.id, status=o.status, filled=o.filled,
                           avg_price=o.avg_price, qty=o.req.qty, perm_id=o.id,
                           message=message, ref=o.req.ref)

    def _emit(self, o: _SimOrder, message: str = "") -> None:
        self._queue.append(("order", self._update(o, message)))

    def _match(self, symbol: str, bar: Bar) -> None:
        live = [o for o in self.orders.values()
                if o.status == O_SUBMITTED and o.req.symbol == symbol and o.placed_at <= bar.ts]
        # stops first: when a bar spans both levels assume the worse outcome
        live.sort(key=lambda o: 0 if o.req.type in ("STP", "TRAIL") else 1)
        for o in live:
            if o.status != O_SUBMITTED:      # cancelled by an OCA sibling
                continue
            r = o.req
            if not bar.rth and not r.outside_rth:
                continue
            price: Optional[float] = None
            if r.type == "MKT":
                price = bar.open
            elif r.type == "LMT" and r.side == "BUY" and bar.low <= r.limit_price:
                price = min(r.limit_price, bar.open)
            elif r.type == "LMT" and r.side == "SELL" and bar.high >= r.limit_price:
                price = max(r.limit_price, bar.open)
            elif r.type == "STP" and r.side == "SELL" and bar.low <= r.stop_price:
                price = min(r.stop_price, bar.open)
            elif r.type == "TRAIL" and r.side == "SELL":
                if bar.low <= o.stop:
                    price = min(o.stop, bar.open)
                else:
                    o.stop = max(o.stop, round(bar.high * (1 - r.trail_pct / 100), 2))
                    if bar.close <= o.stop:
                        price = o.stop
            if price is not None:
                self._fill(o, price, bar.ts)

    def _fill(self, o: _SimOrder, price: float, ts: int) -> None:
        r = o.req
        fee = max(1.0, 0.005 * r.qty) if self.commissions else 0.0
        if r.side == "BUY":
            if r.qty * price + fee > self.account().available + 1e-6:   # type: ignore[union-attr]
                o.status = O_REJECTED
                self._emit(o, "insufficient settled cash")
                return
            self.cash -= r.qty * price + fee
            self.pos[r.symbol] = self.pos.get(r.symbol, 0) + r.qty
        else:
            if self.pos.get(r.symbol, 0) < r.qty:
                o.status = O_REJECTED
                self._emit(o, "no shares to sell")
                return
            proceeds = r.qty * price - fee
            self.cash += proceeds
            self.pos[r.symbol] -= r.qty
            day = to_et(ts).date()
            self.unsettled.append((self.calendar.next_trading_day(day), proceeds))
        o.status, o.filled, o.avg_price = O_FILLED, r.qty, price
        self._exec_seq += 1
        self._queue.append(("exec", Execution(o.id, f"X{self._exec_seq}", r.qty, price, ts + 30,
                                              fee if self.commissions else None)))
        self._emit(o)
        if r.oca_group:
            for other in self.orders.values():
                if other is not o and other.status == O_SUBMITTED and other.req.oca_group == r.oca_group:
                    other.status = O_CANCELLED
                    self._emit(other, "OCA")


# ---------------------------------------------------------------------------
# bar sources: iterators of (minute start ts, {symbol: Bar})

Step = tuple[int, dict[str, Bar]]


def _ts(d: dt.date, t: dt.time) -> int:
    return int(dt.datetime.combine(d, t, tzinfo=ET).timestamp())


def session_minutes(calendar: MarketCalendar, d: dt.date, pre_minutes: int = 90,
                    post_minutes: int = 120) -> list[tuple[int, bool]]:
    """(ts, is_rth) for every simulated minute of the trading day ``d``."""
    sess = calendar.session(d)
    if sess is None:
        return []
    start = int(sess.open.timestamp()) - pre_minutes * 60
    end = int(sess.close.timestamp()) + post_minutes * 60
    o, c = int(sess.open.timestamp()), int(sess.close.timestamp())
    return [(t, o <= t < c) for t in range(start, end, 60)]


def synthetic_day(calendar: MarketCalendar, d: dt.date, prices: dict[str, float],
                  rng: random.Random) -> Iterator[Step]:
    """Random-walk bars for one day; ``prices`` is updated in place."""
    drift = {s: rng.uniform(-0.00002, 0.00012) for s in prices}
    # (market, tech) loadings and own noise, as a share of the bar volatility
    index = {"SPY": (1.0, 0.0, 0.15), "QQQ": (1.0, 0.9, 0.15), "DIA": (0.9, -0.5, 0.2), "IWM": (1.1, 0.1, 0.5)}
    for ts, rth in session_minutes(calendar, d):
        step: dict[str, Bar] = {}
        vol = 0.0011 if rth else 0.0005
        market, tech = rng.gauss(0, vol * 0.45), rng.gauss(0, vol * 0.45)
        for s in prices:
            if not rth and rng.random() < 0.55:       # thin extended-hours tape
                continue
            m, t, own = index.get(s, (1.2, 1.0, 0.75))
            o = prices[s]
            c = o * math.exp(drift[s] * (0.3 if s in index else 1) + m * market + t * tech + rng.gauss(0, vol * own))
            hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
            prices[s] = c
            step[s] = Bar(ts, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2),
                          float(rng.randint(500, 20000) if rth else rng.randint(50, 900)), rth)
        yield ts, step
    sess = calendar.session(d)
    if sess is not None:
        # empty steps so the engine sees the end of after-hours and the next morning
        yield int(sess.post_close.timestamp()) + 60, {}
        nxt = calendar.session(calendar.next_trading_day(d))
        yield int(nxt.pre_open.timestamp()), {}        # type: ignore[union-attr]


def recorded_day(calendar: MarketCalendar, d: dt.date, bars: dict[str, list[Bar]]) -> Iterator[Step]:
    """Replay recorded bars of the ET date ``d``."""
    by_ts: dict[int, dict[str, Bar]] = {}
    lo, hi = _ts(d, dt.time(0, 0)), _ts(d + dt.timedelta(days=1), dt.time(0, 0))
    for sym, series in bars.items():
        for b in series:
            if lo <= b.ts < hi:
                by_ts.setdefault(b.ts, {})[sym] = b
    for ts in sorted(by_ts):
        yield ts, by_ts[ts]
    sess = calendar.session(d)
    if sess is not None:
        yield int(sess.post_close.timestamp()) + 60, {}
        nxt = calendar.session(calendar.next_trading_day(d))
        yield int(nxt.pre_open.timestamp()), {}        # type: ignore[union-attr]
