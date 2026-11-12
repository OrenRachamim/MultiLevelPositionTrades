"""Offline checks of the IB adapter's translation logic.

These do NOT replace a paper-account test: they only verify that IB objects
are mapped to engine events the way the engine expects.
"""

from __future__ import annotations

import datetime as dt

import pytest

ib_async = pytest.importorskip("ib_async")

from ib_async import (AccountValue, BarData, BarDataList, CommissionReport, Execution, Fill, Order,  # noqa: E402
                      OrderStatus, Position, Stock, Trade, TradeLogEntry)

from mlpt.broker.ib import IBBroker  # noqa: E402
from mlpt.config import IBConfig  # noqa: E402
from mlpt.market_calendar import MarketCalendar  # noqa: E402
from mlpt.models import O_CANCELLED, O_FILLED, O_REJECTED, O_SUBMITTED, OrderRequest  # noqa: E402

NOW = dt.datetime(2026, 11, 10, 15, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def broker() -> IBBroker:
    b = IBBroker(IBConfig(client_id=17, account="DU123"), "paper", MarketCalendar())
    b._synced = True
    b.connected = True
    return b


def trade(status: str, filled: float = 0, avg: float = 0, qty: float = 10, order_id: int = 5,
          ref: str = "MLPT-1-ENTRY", log: list | None = None, fills: list | None = None, perm: int = 900) -> Trade:
    order = Order(orderId=order_id, clientId=17, permId=perm, action="BUY", totalQuantity=qty, orderRef=ref)
    return Trade(Stock("AAPL", "SMART", "USD"), order,
                 OrderStatus(orderId=order_id, status=status, filled=filled, avgFillPrice=avg, permId=perm),
                 fills or [], log or [])


def fill(shares: float, price: float, exec_id: str = "e1") -> Fill:
    return Fill(Stock("AAPL", "SMART", "USD"), Execution(execId=exec_id, shares=shares, price=price, orderId=5, time=NOW),
                CommissionReport(), NOW)


def test_status_mapping(broker: IBBroker):
    assert broker._update(trade("PreSubmitted")).status == O_SUBMITTED
    assert broker._update(trade("Submitted", filled=4, avg=100)).filled == 4
    u = broker._update(trade("Filled", filled=10, avg=100.5))
    assert (u.status, u.filled, u.avg_price, u.broker_order_id, u.perm_id) == (O_FILLED, 10, 100.5, "5", "900")
    assert broker._update(trade("Cancelled")).status == O_CANCELLED
    assert broker._update(trade("Cancelled", filled=3, avg=100)).status == O_CANCELLED     # partial then cancelled
    assert broker._update(trade("Inactive")).status == O_REJECTED


def test_warning_on_live_order_is_not_a_rejection(broker: IBBroker):
    # ib_async reports warnings (e.g. 399 "will not be placed until the open") as ValidationError
    log = [TradeLogEntry(NOW, "ValidationError", "Warning 399 ...", 399)]
    assert broker._update(trade("ValidationError", log=log)).status == O_SUBMITTED


def test_rejection_and_plain_cancel(broker: IBBroker):
    rejected = trade("Cancelled", log=[TradeLogEntry(NOW, "Cancelled", "Error 201: Order rejected - no cash", 201)])
    u = broker._update(rejected)
    assert u.status == O_REJECTED and "201" in u.message
    cancelled = trade("Cancelled", log=[TradeLogEntry(NOW, "Cancelled", "Error 202: Order Canceled", 202)])
    assert broker._update(cancelled).status == O_CANCELLED


def test_executions_arriving_before_the_status(broker: IBBroker):
    t = trade("Submitted", fills=[fill(4, 100.0, "a"), fill(6, 101.0, "b")])
    u = broker._update(t)
    assert (u.status, u.filled) == (O_FILLED, 10) and u.avg_price == pytest.approx(100.6)


def test_only_our_orders_are_reported(broker: IBBroker):
    seen = []
    broker.on_order = seen.append
    broker._on_trade(trade("Submitted", ref="manual order"))
    broker._on_trade(trade("Submitted", ref=""))
    assert seen == []
    broker._on_trade(trade("Submitted"))
    assert len(seen) == 1
    # an order completed in an earlier session has no order id: it is matched by its permanent id
    old = trade("Filled", order_id=0, perm=777)
    old.order.filledQuantity = 10
    u = broker._update(old)
    assert (u.broker_order_id, u.perm_id, u.status, u.filled, u.avg_price) == ("perm:777", "777", O_FILLED, 10, 0)


def test_execution_and_commission_events(broker: IBBroker):
    execs, orders = [], []
    broker.on_execution, broker.on_order = execs.append, orders.append
    f = fill(10, 100.0)
    t = trade("Submitted", fills=[f])
    broker._on_exec(t, f)
    broker._on_commission(t, f, CommissionReport(execId="e1", commission=1.0))
    assert [(e.exec_id, e.qty, e.price, e.commission) for e in execs] == [("e1", 10, 100.0, None), ("e1", 10, 100.0, 1.0)]
    assert execs[0].ts == int(NOW.timestamp()) and orders[0].status == O_FILLED


def test_order_construction(broker: IBBroker, monkeypatch):
    placed = []

    def fake_place(contract, order):
        order.orderId = 40 + len(placed)
        placed.append(order)
        return Trade(contract, order, OrderStatus(orderId=order.orderId, status="PendingSubmit"))

    monkeypatch.setattr(broker.ib, "placeOrder", fake_place)
    with pytest.raises(LookupError):
        broker.place(OrderRequest("AAPL", "BUY", 10, "LMT", limit_price=100.1))        # contract not qualified yet
    broker._contracts["AAPL"] = Stock("AAPL", "SMART", "USD")
    ids = broker.place_oca([
        OrderRequest("AAPL", "SELL", 10, "LMT", limit_price=100.5, tif="GTC", outside_rth=True, oca_group="G", ref="MLPT-1-TP"),
        OrderRequest("AAPL", "SELL", 10, "STP", stop_price=96.0, tif="GTC", oca_group="G", ref="MLPT-1-SL"),
    ])
    tp, sl = placed
    assert ids == ["40", "41"]
    assert (tp.action, tp.orderType, tp.lmtPrice, tp.tif, tp.outsideRth, tp.ocaGroup, tp.ocaType) == \
        ("SELL", "LMT", 100.5, "GTC", True, "G", 1)
    assert (sl.orderType, sl.auxPrice, sl.outsideRth, sl.ocaGroup, sl.account, sl.orderRef) == \
        ("STP", 96.0, False, "G", "DU123", "MLPT-1-SL")
    assert tp.transmit and sl.transmit and tp.totalQuantity == 10
    trail_id = broker.place(OrderRequest("AAPL", "SELL", 10, "TRAIL", stop_price=100.4, trail_pct=0.2, tif="GTC",
                                         ref="MLPT-1-INTRADAY-2-TRAIL"))
    trail = placed[-1]
    assert trail_id == "42" and (trail.orderType, trail.trailingPercent, trail.trailStopPrice) == ("TRAIL", 0.2, 100.4)
    assert (trail.action, trail.tif, trail.ocaGroup, trail.outsideRth, trail.totalQuantity) == ("SELL", "GTC", "", False, 10)
    broker.connected = False
    with pytest.raises(ConnectionError):
        broker.place(OrderRequest("AAPL", "BUY", 1, "MKT"))


def test_account_uses_the_most_conservative_cash_figure(broker: IBBroker, monkeypatch):
    def values(rows):
        monkeypatch.setattr(broker.ib, "accountValues", lambda account="": [AccountValue("DU123", *r, "") for r in rows])

    values([("NetLiquidation", "50000", "USD"), ("AvailableFunds", "20000", "USD"), ("SettledCash", "12000", "USD"),
            ("TotalCashValue", "20000", "USD"), ("CashBalance", "20000", "USD"), ("CashBalance", "20000", "BASE")])
    a = broker.account()
    assert (a.net_liq, a.available, a.account) == (50000, 12000, "DU123")
    # base currency ILS: values are converted, and only the USD balance can buy US stocks
    values([("NetLiquidation", "185000", "ILS"), ("AvailableFunds", "74000", "ILS"), ("ExchangeRate", "3.7", "USD"),
            ("CashBalance", "5000", "USD"), ("CashBalance", "55500", "ILS")])
    a = broker.account()
    assert a.net_liq == pytest.approx(50000) and a.available == 5000
    values([("AvailableFunds", "1", "USD")])
    assert broker.account() is None
    broker._synced = False
    assert broker.account() is None and broker.positions() is None and broker.snapshot_orders() is None


def test_positions_and_symbol_mapping(broker: IBBroker, monkeypatch):
    monkeypatch.setattr(broker.ib, "positions", lambda account="": [
        Position("DU123", Stock("AAPL", "SMART", "USD"), 25, 100.0),
        Position("DU123", Stock("BRK B", "SMART", "USD"), 3, 400.0),
        Position("DU123", Stock("OLD", "SMART", "USD"), 0, 0.0)])
    assert broker.positions() == {"AAPL": 25.0, "BRK.B": 3.0}
    assert IBBroker._ib_symbol("BRK.B") == "BRK B"


def test_bar_stream_emits_each_completed_bar_once(broker: IBBroker):
    out = []
    broker.on_bar = lambda sym, bar, done: out.append((bar.ts, bar.close, done))
    t0 = dt.datetime(2026, 11, 10, 14, 30, tzinfo=dt.timezone.utc)
    mk = lambda i, c: BarData(date=t0 + dt.timedelta(minutes=i), open=c, high=c, low=c, close=c, volume=100)
    bars = BarDataList([mk(0, 10.0), mk(1, 11.0)])
    broker._subs["AAPL"] = bars
    broker._emitted["AAPL"] = int(t0.timestamp())                  # bar 0 went out with the back-fill
    broker._on_bars("AAPL", bars, False)                           # bar 1 still forming
    assert out == [(int(t0.timestamp()) + 60, 11.0, False)]
    out.clear()
    bars[-1] = mk(1, 11.5)
    bars.append(mk(2, 12.0))
    broker._on_bars("AAPL", bars, True)                            # bar 2 opened: bar 1 is complete
    assert out == [(int(t0.timestamp()) + 60, 11.5, True), (int(t0.timestamp()) + 120, 12.0, False)]
    out.clear()
    bars.append(mk(5, 13.0))                                       # a gap: nothing is emitted twice
    broker._on_bars("AAPL", bars, True)
    assert [(o[0] - int(t0.timestamp())) // 60 for o in out] == [2, 5] and [o[2] for o in out] == [True, False]
    broker._on_bars("AAPL", BarDataList([mk(9, 1.0)]), True)       # stale subscription object: ignored
    assert len(out) == 2
