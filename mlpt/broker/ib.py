"""Interactive Brokers adapter (IB Gateway / TWS API through ``ib_async``).

NOTE: this adapter cannot be exercised without a running IB Gateway. It was
written against the ib_async 2.x API and must be verified on a PAPER account
before it is trusted with real money (see README, "בדיקות לפני מסחר אמיתי").

Design notes
* One-minute bars come from ``reqHistoricalData(keepUpToDate=True)``: a
  two-day back-fill (warm-up for the moving averages) followed by live updates.
* Orders are tagged with ``orderRef = MLPT-...``; only those are reported to
  the engine, so manual trades in the same account are left alone.
* Keep ``client_id`` constant: open orders are bound to the client id.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any, Optional

from ..config import IBConfig
from ..market_calendar import MarketCalendar
from ..models import (O_CANCELLED, O_FILLED, O_REJECTED, O_SUBMITTED, AccountSnapshot, Bar,
                      Execution, OrderRequest, OrderUpdate)
from .base import Broker

log = logging.getLogger("mlpt.ib")

REF_PREFIX = "MLPT-"
OCA_CANCEL_WITH_BLOCK = 1
STALE_FEED_SEC = 180
# informational codes that need no attention
# trade-log codes that do not mean "the order was rejected"
NOT_A_REJECTION = {0, 202, 10147, 10148, 105, 165, 321, 329, 399, 404, 434, 492, 10167} | set(range(2100, 2200))
QUIET_CODES = {202, 2100, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2157, 2158, 10147, 10148}
CONNECTIVITY_LOST, CONNECTIVITY_RESTORED_DATA_LOST = 1100, 1101


def _bar_ts(d: Any) -> int:
    if isinstance(d, dt.datetime):
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp())
    raise ValueError(f"unexpected bar date {d!r}")


class IBBroker(Broker):
    name = "ib"

    def __init__(self, cfg: IBConfig, mode: str, calendar: MarketCalendar):
        super().__init__()
        from ib_async import IB   # imported lazily: not needed for sim / tests

        self.cfg = cfg
        self.mode = mode
        self.calendar = calendar
        self.ib = IB()
        self.account_id = cfg.account
        self.fatal: Optional[str] = None
        self._synced = False
        self._contracts: dict[str, Any] = {}
        self._want: set[str] = set()
        self._subs: dict[str, Any] = {}             # symbol -> BarDataList
        self._emitted: dict[str, int] = {}          # symbol -> ts of last completed bar sent
        self._last_update: dict[str, float] = {}
        self._pending: set[str] = set()             # subscriptions being set up
        self._last_attempt: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._acct_ts = 0.0

        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.orderStatusEvent += self._on_trade
        self.ib.openOrderEvent += self._on_trade
        self.ib.execDetailsEvent += self._on_exec
        self.ib.commissionReportEvent += self._on_commission
        self.ib.errorEvent += self._on_error
        self.ib.accountValueEvent += self._on_account_value

    # ------------------------------------------------------------ connection
    async def run(self) -> None:
        """Connection supervisor: connect, reconnect, watch for stale feeds."""
        delay = 3.0
        while self.fatal is None:
            if not self.ib.isConnected():
                try:
                    await self._connect()
                    delay = 3.0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("IB connect failed: %s: %s", type(e).__name__, e)
                    self.on_error(f"ההתחברות ל-IB Gateway נכשלה ({type(e).__name__}) - ניסיון חוזר", None)
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.7, 30.0)
                    continue
            else:
                self._check_feeds()
            await asyncio.sleep(2.0)
        while True:      # fatal configuration problem: stay up so the dashboard can show it
            await asyncio.sleep(3600)

    async def _connect(self) -> None:
        await self.ib.connectAsync(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id,
                                   timeout=20, account=self.cfg.account)
        accounts = self.ib.managedAccounts()
        if not self.account_id:
            if len(accounts) != 1:
                self._fatal(f"נמצאו כמה חשבונות ({', '.join(accounts)}) - יש להגדיר ib.account ב-config.toml")
                return
            self.account_id = accounts[0]
        elif self.account_id not in accounts:
            self._fatal(f"החשבון {self.account_id} לא נמצא בחיבור הזה ({', '.join(accounts)})")
            return
        is_paper = self.account_id.upper().startswith("D")
        if self.mode == "paper" and not is_paper:
            self._fatal(f"mode=paper אבל החשבון {self.account_id} הוא חשבון אמיתי - המערכת לא תסחר. "
                        f"לשימוש בחשבון אמיתי יש להגדיר mode = \"live\"")
            return
        if self.mode == "live" and is_paper:
            self.on_error(f"mode=live אבל החשבון {self.account_id} הוא חשבון Paper", None)
        try:
            await asyncio.wait_for(self.ib.reqAccountSummaryAsync(), 10)
        except Exception:
            log.warning("account summary request failed; using account updates only")
        self._synced = True
        self.connected = True
        self.on_connection(True, f"({self.account_id}, {self.cfg.host}:{self.cfg.port})")
        for sym in sorted(self._want):
            self._ensure(sym)

    def _fatal(self, message: str) -> None:
        self.fatal = message
        log.error("FATAL: %s", message)
        self.on_error(message, None)
        self.ib.disconnect()

    def _on_disconnected(self) -> None:
        was = self.connected
        self.connected = False
        self._synced = False
        self._subs.clear()
        if was:
            self.on_connection(False, "")

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self.ib.isConnected():
            self.ib.disconnect()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ market data
    @staticmethod
    def _ib_symbol(symbol: str) -> str:
        return symbol.replace(".", " ").replace("-", " ")       # BRK.B -> "BRK B"

    async def _contract(self, symbol: str) -> Any:
        from ib_async import Stock

        if symbol not in self._contracts:
            found = await self.ib.qualifyContractsAsync(Stock(self._ib_symbol(symbol), "SMART", "USD"))
            found = [c for c in found if c is not None and getattr(c, "conId", 0)]
            if not found:
                raise LookupError(f"הסימול {symbol} לא זוהה ב-IBKR")
            self._contracts[symbol] = found[0]
        return self._contracts[symbol]

    def ready(self, symbol: str) -> bool:
        return symbol in self._contracts

    def subscribe(self, symbol: str) -> None:
        self._want.add(symbol)
        self._ensure(symbol)

    def _ensure(self, symbol: str) -> None:
        """Start a subscription unless one exists or is already being set up."""
        if self.connected and symbol not in self._subs and symbol not in self._pending:
            self._pending.add(symbol)
            self._last_attempt[symbol] = time.time()
            self._spawn(self._subscribe(symbol))

    def unsubscribe(self, symbol: str) -> None:
        self._want.discard(symbol)
        bars = self._subs.pop(symbol, None)
        if bars is not None and self.ib.isConnected():
            try:
                self.ib.cancelHistoricalData(bars)
            except Exception:
                log.debug("cancelHistoricalData failed", exc_info=True)

    async def _subscribe(self, symbol: str) -> None:
        try:
            contract = await self._contract(symbol)
            bars = await self.ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr="2 D", barSizeSetting="1 min",
                whatToShow="TRADES", useRTH=False, formatDate=2, keepUpToDate=True, timeout=60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.on_error(f"בקשת הנרות נכשלה: {e}", symbol)
            return
        finally:
            self._pending.discard(symbol)
        if symbol not in self._want or not self.connected or symbol in self._subs:
            if bars and self.ib.isConnected():          # no longer needed: release the data line
                self.ib.cancelHistoricalData(bars)
            return
        if not bars:
            self.on_error("לא התקבלו נרות (האם יש מנוי לנתוני זמן אמת?) - ניסיון חוזר בעוד דקה", symbol)
            return
        self._subs[symbol] = bars
        self._last_update[symbol] = time.time()
        done = [self._bar(b) for b in bars[:-1]]
        if done:
            self.on_history(symbol, done)
            self._emitted[symbol] = done[-1].ts
        self.on_bar(symbol, self._bar(bars[-1]), False)
        bars.updateEvent += lambda b, has_new, s=symbol: self._on_bars(s, b, has_new)

    async def fetch_history(self, symbol: str, start: dt.date, end: dt.date, chunk_days: int = 5,
                            pause: float = 1.5) -> list[Bar]:
        """One-minute bars (regular + extended hours) for every day from ``start`` to ``end``.

        IB serves intraday history in chunks, walking backwards from an end time.
        """
        from ..market_calendar import ET

        contract = await self._contract(symbol)
        lo = int(dt.datetime.combine(start, dt.time(0), tzinfo=ET).timestamp())
        hi = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), tzinfo=ET).timestamp())
        cursor = dt.datetime.fromtimestamp(hi, dt.timezone.utc)
        found: dict[int, Bar] = {}
        failures = 0
        while cursor.timestamp() > lo:
            try:
                chunk = await self.ib.reqHistoricalDataAsync(
                    contract, endDateTime=cursor, durationStr=f"{chunk_days} D", barSizeSetting="1 min",
                    whatToShow="TRADES", useRTH=False, formatDate=2, keepUpToDate=False, timeout=120)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                chunk = None
                log.warning("history request for %s failed: %s", symbol, e)
            if not chunk:
                failures += 1
                if failures > 3:
                    break                       # nothing (more) available for this stock
                if chunk_days > 1:
                    chunk_days = 1              # some accounts only serve short requests
                else:                           # an empty day (holiday, halt) or pacing: step back and wait
                    cursor -= dt.timedelta(days=1)
                    await asyncio.sleep(pause * 4)
                continue
            failures = 0
            bars = [self._bar(b) for b in chunk]
            for b in bars:
                if lo <= b.ts < hi:
                    found[b.ts] = b
            earliest = dt.datetime.fromtimestamp(bars[0].ts, dt.timezone.utc)
            if earliest >= cursor:
                break                           # no progress: avoid looping forever
            cursor = earliest
            await asyncio.sleep(pause)          # stay well inside IB's request pacing
        return [found[t] for t in sorted(found)]

    @staticmethod
    def _bar(b: Any) -> Bar:
        return Bar(_bar_ts(b.date), b.open, b.high, b.low, b.close, float(b.volume or 0))

    def _on_bars(self, symbol: str, bars: Any, has_new: bool) -> None:
        if self._subs.get(symbol) is not bars or not bars:
            return
        self._last_update[symbol] = time.time()
        if has_new and len(bars) >= 2:
            last_sent = self._emitted.get(symbol, 0)
            fresh = []
            for b in reversed(bars[:-1]):
                ts = _bar_ts(b.date)
                if ts <= last_sent:
                    break
                fresh.append(self._bar(b))
            for bar in reversed(fresh):
                self.on_bar(symbol, bar, True)
                self._emitted[symbol] = bar.ts
        self.on_bar(symbol, self._bar(bars[-1]), False)

    def _check_feeds(self) -> None:
        """Retry failed subscriptions; re-subscribe feeds that went silent during RTH."""
        if not self.connected:
            return
        now = time.time()
        rth = self.calendar.is_rth_ts(int(now))
        for sym in sorted(self._want):
            if sym in self._pending:
                continue
            if sym not in self._subs:
                if now - self._last_attempt.get(sym, 0) > 60:
                    self._ensure(sym)
            elif rth and now - self._last_update.get(sym, 0) > STALE_FEED_SEC:
                self._last_update[sym] = now
                log.warning("bar feed of %s is silent - re-subscribing", sym)
                self._resubscribe(sym)

    def _resubscribe(self, symbol: str) -> None:
        bars = self._subs.pop(symbol, None)
        if bars is not None:
            try:
                self.ib.cancelHistoricalData(bars)
            except Exception:
                log.debug("cancelHistoricalData failed", exc_info=True)
        self._ensure(symbol)

    # ---------------------------------------------------------------- account
    def _on_account_value(self, _value: Any) -> None:
        self._acct_ts = time.time()

    def account(self) -> Optional[AccountSnapshot]:
        if not self._synced:
            return None
        vals: dict[tuple[str, str], float] = {}
        rows = list(self.ib.wrapper.acctSummary.values()) + list(self.ib.accountValues(self.account_id))
        for v in rows:
            if v.account != self.account_id:
                continue
            try:
                vals[(v.tag, v.currency)] = float(v.value)
            except (TypeError, ValueError):
                continue
        base = next((c for (tag, c) in vals if tag == "NetLiquidation" and c and c != "BASE"), None)
        if base is None:
            return None
        usd_rate = 1.0 if base == "USD" else vals.get(("ExchangeRate", "USD"), 0.0)   # 1 USD in base
        if usd_rate <= 0:
            return None
        net_liq = vals[("NetLiquidation", base)] / usd_rate
        # the most conservative figure wins: only settled USD cash can be spent
        candidates = {tag: vals[(tag, base)] / usd_rate
                      for tag in ("SettledCash", "AvailableFunds", "TotalCashValue") if (tag, base) in vals}
        for tag in ("CashBalance", "TotalCashBalance"):
            if (tag, "USD") in vals:
                candidates[f"{tag}(USD)"] = vals[(tag, "USD")]
        if not candidates:
            return None
        return AccountSnapshot(net_liq=net_liq, available=min(candidates.values()), account=self.account_id,
                               as_of=int(self._acct_ts), detail={"base": base, **candidates})

    def positions(self) -> Optional[dict[str, float]]:
        if not self._synced:
            return None
        out: dict[str, float] = {}
        for p in self.ib.positions(self.account_id):
            if p.contract.secType == "STK" and p.position:
                sym = p.contract.symbol.replace(" ", ".")
                out[sym] = out.get(sym, 0.0) + float(p.position)
        return out

    # ----------------------------------------------------------------- orders
    def place(self, req: OrderRequest) -> str:
        from ib_async import Order

        if not self.connected:
            raise ConnectionError("אין חיבור ל-IB Gateway")
        contract = self._contracts.get(req.symbol)
        if contract is None:
            raise LookupError(f"אין חוזה מזוהה עבור {req.symbol}")
        order = Order(action=req.side, totalQuantity=req.qty, orderType=req.type, tif=req.tif,
                      outsideRth=req.outside_rth, orderRef=req.ref, account=self.account_id, transmit=True)
        if req.type == "LMT":
            order.lmtPrice = req.limit_price
        elif req.type == "STP":
            order.auxPrice = req.stop_price
        elif req.type == "TRAIL":
            # native trailing stop: IB moves the stop itself, tick by tick
            order.trailingPercent = req.trail_pct
            order.trailStopPrice = req.stop_price
        if req.oca_group:
            order.ocaGroup = req.oca_group
            order.ocaType = OCA_CANCEL_WITH_BLOCK
        trade = self.ib.placeOrder(contract, order)
        return str(trade.order.orderId)

    def _find_trade(self, broker_order_id: str) -> Any:
        for t in self.ib.trades():
            if self._ours(t) and self._order_key(t) == broker_order_id:
                return t
        return None

    def cancel(self, broker_order_id: str) -> None:
        if not self.connected:
            raise ConnectionError("אין חיבור ל-IB Gateway")
        trade = self._find_trade(broker_order_id)
        if trade is None:
            raise LookupError(f"הפקודה {broker_order_id} לא נמצאה אצל הברוקר")
        if not trade.isDone():
            self.ib.cancelOrder(trade.order)

    def _ours(self, trade: Any) -> bool:
        o = trade.order
        return (o.orderRef or "").startswith(REF_PREFIX) and o.clientId in (0, self.cfg.client_id) \
            and bool(o.orderId or o.permId)

    @staticmethod
    def _order_key(trade: Any) -> str:
        # orders completed in an earlier session come back without an order id
        o = trade.order
        return str(o.orderId) if o.orderId else f"perm:{o.permId or trade.orderStatus.permId}"

    def _update(self, trade: Any) -> OrderUpdate:
        st = trade.orderStatus
        filled, avg = float(st.filled or 0), float(st.avgFillPrice or 0)
        shares = sum(float(f.execution.shares) for f in trade.fills)
        if shares > filled:      # executions can arrive before the status message
            filled = shares
            avg = sum(float(f.execution.shares) * float(f.execution.price) for f in trade.fills) / shares
        done_qty = float(getattr(trade.order, "filledQuantity", 0) or 0)
        if filled == 0 and 0 < done_qty < 1e12:      # completed-orders report; price unknown
            filled = done_qty
        total = float(trade.order.totalQuantity or 0)
        # a rejection shows up as "Cancelled" plus an error entry in the trade log
        errors = [e for e in trade.log if getattr(e, "errorCode", 0) not in NOT_A_REJECTION]
        message = errors[-1].message if errors else ""
        if st.status == "Filled" or (total and filled >= total):
            status = O_FILLED
        elif st.status in ("Cancelled", "ApiCancelled"):
            status = O_REJECTED if errors and filled == 0 else O_CANCELLED
        elif st.status == "Inactive":
            status = O_REJECTED if filled == 0 else O_CANCELLED
        else:
            # includes "ValidationError": ib_async uses it for warnings on orders that are STILL LIVE
            status = O_SUBMITTED
        perm = st.permId or trade.order.permId
        return OrderUpdate(broker_order_id=self._order_key(trade), status=status, filled=filled,
                           avg_price=avg, qty=total or None, perm_id=str(perm) if perm else None,
                           message=message[:300], ref=trade.order.orderRef or "")

    def snapshot_orders(self) -> Optional[list[OrderUpdate]]:
        if not self._synced:
            return None
        return [self._update(t) for t in self.ib.trades() if self._ours(t)]

    def _on_trade(self, trade: Any) -> None:
        if self._ours(trade):
            self.on_order(self._update(trade))

    def _on_exec(self, trade: Any, fill: Any) -> None:
        if not self._ours(trade):
            return
        ex = fill.execution
        self.on_execution(Execution(self._order_key(trade), ex.execId, float(ex.shares), float(ex.price),
                                    int(ex.time.timestamp()) if ex.time else int(time.time())))
        self.on_order(self._update(trade))

    def _on_commission(self, trade: Any, fill: Any, report: Any) -> None:
        if not self._ours(trade):
            return
        ex = fill.execution
        self.on_execution(Execution(self._order_key(trade), ex.execId, float(ex.shares), float(ex.price),
                                    int(ex.time.timestamp()) if ex.time else int(time.time()),
                                    commission=float(report.commission or 0)))

    def _on_error(self, req_id: int, code: int, message: str, contract: Any = None) -> None:
        if code in QUIET_CODES:
            return
        symbol = getattr(contract, "symbol", None) if contract is not None else None
        if code == 162 and "cancel" in message.lower():
            return                              # our own cancelHistoricalData
        if code == CONNECTIVITY_RESTORED_DATA_LOST:
            for sym in sorted(self._want):
                self._resubscribe(sym)
        trade = next((t for t in self.ib.trades() if t.order.orderId == req_id and self._ours(t)), None) \
            if req_id and req_id > 0 else None
        if trade is not None:
            # ib_async has already turned a real rejection into a status event (see _update)
            symbol = symbol or trade.contract.symbol
        self.on_error(f"IB [{code}] {message}", symbol.replace(" ", ".") if symbol else None)
        if code == CONNECTIVITY_LOST:
            log.warning("IB Gateway lost its connection to the IB servers")
