"""The trading engine: one synchronous state machine.

Life of a stock on the daily list::

    WATCHING --signal in the entry window--> position ENTERING
        ENTERING --entry filled--> INTRADAY   (small take-profit + stop, OCA at the broker)
        INTRADAY --take-profit / stop--> CLOSED
        INTRADAY --end of after-hours--> SWING  (swing take-profit + stop)
        SWING    --take-profit / stop / max days--> CLOSED

The engine is driven by broker callbacks (bars, order updates, executions) and
by ``tick()``. It never blocks. Protective orders live at the broker, and
``_manage`` continuously makes the broker's working orders match what each
open position *should* have - that single mechanism covers the first
placement, the intraday->swing switch, partial fills and recovery after a
restart.
"""

from __future__ import annotations

import bisect
import datetime as dt
import logging
from typing import Any, Callable, Optional

from .broker.base import Broker
from .config import Settings, parse_hhmm
from .db import Store
from .market_calendar import ET, MarketCalendar, Session, to_et
from .models import (CANCELLED, CLOSED, ENTERING, HOLDING_STATES, INTRADAY, O_LOST, O_REJECTED,
                     O_SUBMITTED, OPEN_STATES, R_EXTERNAL, R_MANUAL, R_SL, R_TIME, R_TP, ROLE_ENTRY, ROLE_EXIT, ROLE_SL, ROLE_TP, SWING, TERMINAL,
                     W_ENTERED, W_NO_SIGNAL, W_SKIPPED, W_WATCHING, AccountSnapshot, Bar,
                     Execution, OrderRequest, OrderUpdate, round_price)
from .notifier import Notifier
from .strategy import STRATEGIES, Signal, moving_average
from .watchlist import PARAM_COLS, parse_watchlist

log = logging.getLogger("mlpt.engine")

PHASE_HE = {INTRADAY: "תוך-יומי", SWING: "סווינג"}
REASON_HE = {R_TP: "Take Profit", R_SL: "Stop Loss", R_MANUAL: "סגירה ידנית",
             R_TIME: "מקסימום ימי החזקה", R_EXTERNAL: "נסגרה מחוץ למערכת"}


def usd(x: float) -> str:
    return f"${x:,.2f}"


def signed_usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


class Engine:
    MAX_EXIT_PLACEMENTS = 8      # per position and phase, then automation halts
    CANCEL_RETRY_SEC = 15
    RESYNC_SEC = 60
    EXIT_LIMIT_TIMEOUT_SEC = 60
    EXIT_LIMIT_DISCOUNT_PCT = 0.5
    WINDOW_GRACE_SEC = 90
    MISMATCH_ALERT_SEC = 30
    TIME_EXIT_BEFORE_CLOSE_SEC = 600
    SERIES_MAX = 4000

    def __init__(self, store: Store, broker: Broker, calendar: MarketCalendar,
                 notifier: Notifier, clock: Callable[[], float], mode: str = "sim"):
        self.store = store
        store.clock = clock
        self.broker = broker
        self.calendar = calendar
        self.notifier = notifier
        self.clock = clock
        self.mode = mode
        self.settings: Settings = store.load_settings()
        self.series: dict[str, list[Bar]] = {}     # completed bars per symbol
        self.live: dict[str, Bar] = {}             # bar in progress per symbol
        self.last: dict[str, float] = {}           # last traded price per symbol
        self.subscribed: set[str] = set()
        self._once_at: dict[str, float] = {}
        self._mismatch_since: dict[int, float] = {}
        self._sell_notices: list[tuple[float, int]] = []
        self._need_resync = True
        self._last_resync = 0.0

        broker.on_bar = self._safe(self.on_bar)
        broker.on_history = self._safe(self.on_history)
        broker.on_order = self._safe(self.on_order)
        broker.on_execution = self._safe(self.on_execution)
        broker.on_connection = self._safe(self.on_connection)
        broker.on_error = self._safe(self.on_broker_error)

    # ------------------------------------------------------------------ utils
    def _safe(self, fn: Callable[..., None]) -> Callable[..., None]:
        def wrapper(*a: Any, **k: Any) -> None:
            try:
                fn(*a, **k)
            except Exception:
                log.exception("error in %s", fn.__name__)
                try:
                    self.event("ERROR", "SYSTEM", f"שגיאה פנימית ב-{fn.__name__} - ראה קובץ לוג")
                except Exception:
                    pass
        return wrapper

    def now(self) -> float:
        return self.clock()

    def now_et(self) -> dt.datetime:
        return to_et(self.now())

    def event(self, level: str, kind: str, message: str, symbol: Optional[str] = None) -> None:
        self.store.event_add(int(self.now()), level, kind, message, symbol)
        log.log({"ERROR": logging.ERROR, "WARN": logging.WARNING}.get(level, logging.INFO),
                "[%s] %s%s", kind, f"{symbol}: " if symbol else "", message)

    def _once(self, key: str, ttl: float) -> bool:
        """True at most once per ``ttl`` seconds for ``key``."""
        now = self.now()
        if now - self._once_at.get(key, -1e18) < ttl:
            return False
        self._once_at[key] = now
        return True

    def alert(self, message: str, symbol: Optional[str] = None, key: Optional[str] = None,
              ttl: float = 3600) -> None:
        """Error-level event + Telegram, rate limited per key."""
        if key and not self._once(key, ttl):
            return
        self.event("ERROR", "ALERT", message, symbol)
        self.notifier.notify(f"⚠️ {symbol + ': ' if symbol else ''}{message}")

    def _at(self, day: dt.date, hhmm: str) -> dt.datetime:
        return dt.datetime.combine(day, parse_hhmm(hhmm), tzinfo=ET)

    # ------------------------------------------------------------- market data
    def _merge(self, symbol: str, bar: Bar) -> None:
        s = self.series.setdefault(symbol, [])
        if not s or bar.ts > s[-1].ts:
            s.append(bar)
            if len(s) > self.SERIES_MAX + 500:
                del s[:-self.SERIES_MAX]
        elif bar.ts == s[-1].ts:
            s[-1] = bar
        else:
            i = bisect.bisect_left([b.ts for b in s], bar.ts)
            if i < len(s) and s[i].ts == bar.ts:
                s[i] = bar
            else:
                s.insert(i, bar)

    def _load_series(self, symbol: str) -> None:
        if symbol not in self.series:
            self.series[symbol] = self.store.bars(symbol, int(self.now()) - 6 * 86400)[-self.SERIES_MAX:]
            if self.series[symbol]:
                self.last.setdefault(symbol, self.series[symbol][-1].close)

    def on_history(self, symbol: str, bars: list[Bar]) -> None:
        if not bars:
            return
        self._load_series(symbol)
        for b in bars:
            b.rth = self.calendar.is_rth_ts(b.ts)
        merged = {b.ts: b for b in self.series[symbol]}
        merged.update((b.ts, b) for b in bars)
        self.series[symbol] = [merged[t] for t in sorted(merged)][-self.SERIES_MAX:]
        self.store.save_bars(symbol, bars)
        self.last[symbol] = self.series[symbol][-1].close

    def on_bar(self, symbol: str, bar: Bar, completed: bool) -> None:
        bar.rth = self.calendar.is_rth_ts(bar.ts)
        self.last[symbol] = bar.close
        if not completed:
            self.live[symbol] = bar
            return
        live = self.live.get(symbol)
        if live is not None and live.ts <= bar.ts:
            del self.live[symbol]
        self._load_series(symbol)
        self._merge(symbol, bar)
        self.store.save_bar(symbol, bar)
        if self.now() - (bar.ts + 60) <= self.settings.max_signal_age_sec:
            self._evaluate_entry(symbol, bar)

    # ------------------------------------------------------------------ entry
    def _strategy_bars(self, symbol: str) -> list[Bar]:
        s = self.series.get(symbol, [])
        if not self.settings.use_extended_bars:
            s = [b for b in s if b.rth]
        return s[-600:]

    def _evaluate_entry(self, symbol: str, bar: Bar) -> None:
        st = self.settings
        now_et = self.now_et()
        today = now_et.date()
        sess = self.calendar.session(today)
        if sess is None:
            return
        item = self.store.watch_find(today.isoformat(), symbol)
        if not item or item["status"] != W_WATCHING:
            return
        bar_et = to_et(bar.ts)
        if not (self._at(today, st.entry_start) <= bar_et < self._at(today, st.entry_end)):
            return
        if not bar.rth and not st.use_extended_bars:
            return
        sig = STRATEGIES[st.strategy](self._strategy_bars(symbol), st)
        if sig is not None:
            self._try_enter(item, sig, sess, now_et)

    def params_for(self, item: dict[str, Any]) -> dict[str, Any]:
        """Trade parameters of a list item: its own values, else the defaults."""
        st = self.settings
        p = {c: (item.get(c) if item.get(c) is not None else getattr(st, c)) for c in PARAM_COLS}
        if not p["intraday_sl_pct"]:
            p["intraday_sl_pct"] = p["swing_sl_pct"]
        return p

    def _spent_today(self, today: str) -> tuple[float, float, float]:
        """(spent incl. working entries, working entries only, filled after account snapshot)."""
        acct = self.broker.account()
        as_of = acct.as_of if acct else 0
        spent = pending = unseen = 0.0
        for p in self.store.pos_for_date(today):
            if p["state"] == ENTERING:
                o = next((o for o in self.store.orders_for_position(p["id"]) if o["role"] == ROLE_ENTRY), None)
                if o:
                    amount = o["qty"] * o["limit_price"]
                    spent += amount
                    pending += amount
            else:
                amount = p["qty"] * (p["entry_price"] or 0.0)
                spent += amount
                if (p["entry_time"] or 0) > as_of:
                    unseen += amount
        return spent, pending, unseen

    def available_cash(self, acct: AccountSnapshot, today: str) -> float:
        """Cash that may be used for a new entry right now.

        Cash account: money from today's sales is not settled yet, so today's
        budget is the cash available at the start of the day minus everything
        bought today - never more than what the broker reports right now.
        """
        spent, pending, unseen = self._spent_today(today)
        key = f"sod:{today}"
        sod = self.store.kv_get(key)
        if sod is None:
            sod = acct.available + (spent - pending - unseen)
            self.store.kv_set(key, sod)
        live = acct.available - pending - unseen
        return min(live, sod - spent) - self.settings.cash_reserve_usd

    def _skip(self, item: dict[str, Any], reason: str, notify: bool = False) -> None:
        self.store.watch_update(item["id"], status=W_SKIPPED, note=reason)
        self.event("WARN", "RISK", f"דילוג על כניסה: {reason}", item["symbol"])
        if notify:
            self.notifier.notify(f"⏭ {item['symbol']} דילוג: {reason}")

    def _try_enter(self, item: dict[str, Any], sig: Signal, sess: Session, now_et: dt.datetime) -> None:
        st, sym, today = self.settings, item["symbol"], item["trade_date"]
        if not st.trading_enabled:
            if self._once(f"disabled:{today}:{sym}", 3600):
                self.event("WARN", "SIGNAL", f"אות כניסה ({sig.reason}) אך המסחר מושבת", sym)
            return
        if not self.broker.connected or not self.broker.ready(sym):
            if self._once(f"noconn:{today}:{sym}", 300):
                self.event("WARN", "SIGNAL", f"אות כניסה ({sig.reason}) אך אין חיבור לברוקר", sym)
            return
        open_positions = self.store.pos_in_states(OPEN_STATES)
        if any(p["symbol"] == sym for p in open_positions):
            return self._skip(item, "קיימת כבר פוזיציה פתוחה במניה")
        if st.max_open_positions and len(open_positions) >= st.max_open_positions:
            return self._skip(item, f"הגענו למקסימום פוזיציות פתוחות ({st.max_open_positions})")
        acct = self.broker.account()
        if acct is None or acct.net_liq <= 0:
            if self._once(f"noacct:{today}:{sym}", 300):
                self.event("WARN", "SIGNAL", f"אות כניסה ({sig.reason}) אך אין נתוני חשבון", sym)
            return

        p = self.params_for(item)
        limit = round_price(sig.price * (1 + st.entry_slippage_pct / 100))
        budget = acct.net_liq * p["position_pct"] / 100
        if p["max_position_usd"] and p["max_position_usd"] > 0:
            budget = min(budget, p["max_position_usd"])
        qty = int(budget // limit)
        if qty < 1:
            return self._skip(item, f"תקציב הפוזיציה ({usd(budget)}) קטן ממחיר מניה אחת ({usd(limit)})")
        cost = qty * limit
        avail = self.available_cash(acct, today)
        if cost > avail:
            return self._skip(item, f"אין מספיק מזומן פנוי: נדרש {usd(cost)}, פנוי {usd(max(avail, 0))}",
                              notify=True)

        pos_id = self.store.pos_add(
            symbol=sym, trade_date=today, watch_id=item["id"], state=ENTERING,
            ref_price=sig.price, signal=sig.reason,
            intraday_tp_pct=p["intraday_tp_pct"], intraday_sl_pct=p["intraday_sl_pct"],
            swing_tp_pct=p["swing_tp_pct"], swing_sl_pct=p["swing_sl_pct"],
            max_hold_days=int(p["max_hold_days"] or 0))
        req = OrderRequest(symbol=sym, side="BUY", qty=qty, type="LMT", limit_price=limit, tif="DAY",
                           outside_rth=not sess.is_rth(now_et), ref=f"MLPT-{pos_id}-ENTRY")
        self.store.watch_update(item["id"], status=W_ENTERED, note=sig.reason)
        self.event("INFO", "SIGNAL", f"אות כניסה: {sig.reason} במחיר {usd(sig.price)}", sym)
        if self._submit(pos_id, req, ROLE_ENTRY, INTRADAY) is None:
            self.store.pos_update(pos_id, state=CANCELLED)
            self.store.watch_update(item["id"], status=W_SKIPPED, note="שליחת פקודת הקנייה נכשלה")

    # ----------------------------------------------------------------- orders
    def _submit(self, pos_id: int, req: OrderRequest, role: str, phase: str,
                reason: Optional[str] = None) -> Optional[int]:
        ids = self._submit_group(pos_id, [(req, role)], phase, reason)
        return ids[0] if ids else None

    def _submit_group(self, pos_id: int, items: list[tuple[OrderRequest, str]], phase: str,
                      reason: Optional[str] = None) -> Optional[list[int]]:
        """Record and send orders; several orders are sent as one OCA group."""
        reqs = [r for r, _ in items]
        try:
            broker_ids = self.broker.place_oca(reqs) if len(reqs) > 1 else [self.broker.place(reqs[0])]
        except Exception as e:
            log.exception("order placement failed")
            self.alert(f"שליחת פקודה נכשלה: {e}", reqs[0].symbol, key=f"place:{pos_id}", ttl=60)
            return None
        ids = []
        for (req, role), bid in zip(items, broker_ids):
            ids.append(self.store.order_add(
                position_id=pos_id, symbol=req.symbol, role=role, phase=phase, reason=reason,
                side=req.side, type=req.type, qty=req.qty, limit_price=req.limit_price,
                stop_price=req.stop_price, tif=req.tif, outside_rth=int(req.outside_rth),
                oca_group=req.oca_group, broker_order_id=bid, status=O_SUBMITTED))
            price = req.limit_price if req.type == "LMT" else req.stop_price
            self.event("INFO", "ORDER",
                       f"נשלחה פקודת {role}: {req.side} {req.qty} {req.type}"
                       f"{' ' + usd(price) if price else ''} {req.tif}"
                       f"{' (גם מחוץ לשעות המסחר)' if req.outside_rth else ''}", req.symbol)
        return ids

    def _cancel(self, o: dict[str, Any]) -> None:
        now = self.now()
        if o["cancel_requested_at"] and now - o["cancel_requested_at"] < self.CANCEL_RETRY_SEC:
            return
        self.store.order_update(o["id"], cancel_requested_at=int(now))
        try:
            self.broker.cancel(o["broker_order_id"])
        except Exception as e:
            log.exception("cancel failed")
            self.alert(f"ביטול פקודה נכשל: {e}", o["symbol"], key=f"cancel:{o['id']}", ttl=300)

    def on_order(self, u: OrderUpdate) -> None:
        o = self.store.order_by_broker_id(u.broker_order_id)
        if o is None and u.perm_id:
            o = self.store.order_by_perm_id(u.perm_id)
        if o is None:
            return
        self._apply_order(o, u.status, u.filled, u.avg_price, u.qty, u.perm_id, u.message)

    def _apply_order(self, o: dict[str, Any], status: str, filled: float = 0.0, avg_price: float = 0.0,
                     qty: Optional[float] = None, perm_id: Optional[str] = None, message: str = "") -> None:
        prev_status, prev_filled = o["status"], o["filled"]
        new_filled = max(prev_filled, filled or 0.0)
        if prev_status in TERMINAL and (status not in TERMINAL or new_filled <= prev_filled):
            status = prev_status          # stale or duplicate update
        fields: dict[str, Any] = {}
        if status != prev_status:
            fields["status"] = status
        if new_filled > prev_filled:
            fields["filled"] = new_filled
            if avg_price > 0:
                fields["avg_price"] = avg_price
        if perm_id and not o["perm_id"]:
            fields["perm_id"] = str(perm_id)
        if qty and status == O_SUBMITTED and int(qty) != o["qty"]:
            fields["qty"] = int(qty)
        if message and message != o["message"]:
            fields["message"] = message
        if not fields:
            return
        self.store.order_update(o["id"], **fields)
        if set(fields) <= {"perm_id", "message"}:
            return
        o = self.store.order_get(o["id"])
        pos = self.store.pos_get(o["position_id"])
        if o is None or pos is None:
            return
        sym = o["symbol"]
        if status == O_REJECTED and prev_status != O_REJECTED:
            self.alert(f"פקודת {o['role']} נדחתה: {message or 'ללא פירוט'}", sym, key=f"rej:{o['id']}")
        elif status == O_LOST and prev_status != O_LOST:
            self.event("WARN", "ORDER", f"פקודת {o['role']} לא נמצאה אצל הברוקר", sym)

        if o["role"] == ROLE_ENTRY:
            if pos["state"] == ENTERING:
                if o["status"] in TERMINAL:
                    self._entry_done(pos, o)
            elif new_filled > prev_filled:
                self.alert("מילוי מאוחר של פקודת קנייה אחרי שהפוזיציה כבר נפתחה - נדרשת בדיקה ידנית", sym)
            return

        if new_filled > prev_filled:
            self._exit_fill(pos, o, int(new_filled - prev_filled))
            pos = self.store.pos_get(pos["id"]) or pos
        if pos["state"] in HOLDING_STATES:
            if o["role"] == ROLE_EXIT and o["status"] in TERMINAL and pos["exit_request"]:
                self.store.pos_update(pos["id"], exit_request=None)
                self.event("WARN", "ORDER", "פקודת היציאה לא בוצעה במלואה - ההגנות מוחזרות", sym)
                pos = self.store.pos_get(pos["id"]) or pos
            self._manage(pos)

    def on_execution(self, e: Execution) -> None:
        o = self.store.order_by_broker_id(e.broker_order_id)
        if o is None:
            return
        self.store.fill_upsert(e.exec_id, o["id"], o["position_id"], e.qty, e.price, e.ts, e.commission)
        pos = self.store.pos_get(o["position_id"])
        if pos is None:
            return
        commission = self.store.commission_for_position(pos["id"])
        if abs(commission - (pos["commission"] or 0.0)) > 1e-9:
            self.store.pos_update(pos["id"], commission=commission)
            if pos["state"] == CLOSED and pos["exit_price"] is not None:
                self.store.pos_update(pos["id"], **self._pnl(pos["entry_price"], pos["exit_price"],
                                                             pos["qty"], commission))

    # --------------------------------------------------------- position flow
    def _entry_done(self, pos: dict[str, Any], o: dict[str, Any]) -> None:
        sym, filled = pos["symbol"], int(o["filled"])
        if filled <= 0:
            self.store.pos_update(pos["id"], state=CANCELLED)
            if pos["watch_id"]:
                if o["status"] == O_REJECTED:
                    self.store.watch_update(pos["watch_id"], status=W_SKIPPED,
                                            note=f"פקודת הקנייה נדחתה: {o['message']}")
                else:   # nothing was bought - a later signal may still enter today
                    self.store.watch_update(pos["watch_id"], status=W_WATCHING, note="פקודת הקנייה לא התמלאה")
            self.event("WARN", "ORDER", "פקודת הקנייה לא התמלאה - אין פוזיציה", sym)
            return
        entry = o["avg_price"]
        tp = round_price(entry * (1 + pos["intraday_tp_pct"] / 100))
        sl = round_price(entry * (1 - pos["intraday_sl_pct"] / 100))
        self.store.pos_update(pos["id"], state=INTRADAY, qty=filled, qty_open=filled, entry_price=entry,
                              entry_time=int(self.now()), tp_price=tp, sl_price=sl)
        partial = f" (מילוי חלקי מתוך {o['qty']})" if filled < o["qty"] else ""
        self.event("INFO", "FILL", f"קנייה: {filled} מניות במחיר {usd(entry)}{partial} · "
                                   f"TP {usd(tp)} · SL {usd(sl)}", sym)
        self.notifier.notify(f"🟢 קנייה {sym} · תוך-יומי\n{filled} מניות × {usd(entry)} = {usd(filled * entry)}\n"
                             f"TP {usd(tp)} · SL {usd(sl)}")
        self._manage(self.store.pos_get(pos["id"]))   # type: ignore[arg-type]

    @staticmethod
    def _pnl(entry: float, exit_price: float, qty: int, commission: float) -> dict[str, float]:
        pnl = (exit_price - entry) * qty - commission
        return {"pnl": round(pnl, 2), "pnl_pct": round(pnl / (entry * qty) * 100, 3) if entry and qty else 0.0}

    def _exit_fill(self, pos: dict[str, Any], o: dict[str, Any], delta: int) -> None:
        if pos["state"] not in HOLDING_STATES:
            self.alert(f"מכירה נוספת ({delta} מניות, {o['role']}) אחרי שהפוזיציה כבר נסגרה - "
                       f"נדרשת בדיקה ידנית בחשבון", pos["symbol"])
            return
        exits = [x for x in self.store.orders_for_position(pos["id"]) if x["role"] != ROLE_ENTRY]
        sold = sum(int(x["filled"]) for x in exits)
        qty_open = max(0, pos["qty"] - sold)
        self.store.pos_update(pos["id"], qty_open=qty_open)
        if qty_open > 0:
            self.event("INFO", "FILL", f"מכירה חלקית ({o['role']}): {delta} מניות, נותרו {qty_open}", pos["symbol"])
            return
        def price(x: dict[str, Any]) -> float:
            # the fill price is unknown when the order completed while we were offline
            return x["avg_price"] or x["limit_price"] or x["stop_price"] or self.last.get(pos["symbol"], 0.0)

        exit_price = sum(int(x["filled"]) * price(x) for x in exits) / sold if sold else price(o)
        reason = {ROLE_TP: R_TP, ROLE_SL: R_SL}.get(o["role"], o["reason"] or R_MANUAL)
        self._close(pos, exit_price, reason, o["phase"] or pos["state"])

    def _close(self, pos: dict[str, Any], exit_price: float, reason: str, phase: str) -> None:
        commission = self.store.commission_for_position(pos["id"])
        self.store.pos_update(pos["id"], state=CLOSED, qty_open=0, exit_price=exit_price,
                              exit_time=int(self.now()), exit_reason=reason, exit_phase=phase,
                              exit_request=None, commission=commission,
                              **self._pnl(pos["entry_price"], exit_price, pos["qty"], commission))
        self._mismatch_since.pop(pos["id"], None)
        for o in self.store.orders_for_position(pos["id"]):
            if o["status"] == O_SUBMITTED:
                self._cancel(o)
        self.event("INFO", "FILL", f"מכירה: {pos['qty']} מניות במחיר {usd(exit_price)} · "
                                   f"{PHASE_HE.get(phase, phase)} · {REASON_HE.get(reason, reason)}", pos["symbol"])
        # the commission report arrives a moment after the fill: notify shortly after
        self._sell_notices.append((self.now() + 3, pos["id"]))

    def _send_sell_notice(self, pos_id: int) -> None:
        p = self.store.pos_get(pos_id)
        if p is None:
            return
        icon = "✅" if (p["pnl"] or 0) >= 0 else "🔻"
        approx = " (מחיר משוער)" if p["exit_reason"] == R_EXTERNAL else ""
        self.notifier.notify(
            f"{icon} מכירה {p['symbol']} · {PHASE_HE.get(p['exit_phase'], p['exit_phase'])}\n"
            f"{p['qty']} מניות × {usd(p['exit_price'])}{approx} · {REASON_HE.get(p['exit_reason'], p['exit_reason'])}\n"
            f"רווח: {signed_usd(p['pnl'] or 0)} ({(p['pnl_pct'] or 0):+.2f}%)")

    def _intraday_over(self, pos: dict[str, Any], now_et: dt.datetime) -> bool:
        entry_day = dt.date.fromisoformat(pos["trade_date"])
        return now_et.date() > entry_day or now_et >= self._at(entry_day, self.settings.intraday_end)

    def _to_swing(self, pos: dict[str, Any]) -> dict[str, Any]:
        entry = pos["entry_price"]
        tp = round_price(entry * (1 + pos["swing_tp_pct"] / 100))
        sl = round_price(entry * (1 - pos["swing_sl_pct"] / 100))
        self.store.pos_update(pos["id"], state=SWING, tp_price=tp, sl_price=sl,
                              swing_time=int(self.now()), exit_placements=0)
        self.event("INFO", "POSITION", f"לא הושג הרווח התוך-יומי - מעבר לסווינג · TP {usd(tp)} · SL {usd(sl)}",
                   pos["symbol"])
        self.notifier.notify(f"🔄 {pos['symbol']} עברה לסווינג\nTP {usd(tp)} · SL {usd(sl)}")
        return self.store.pos_get(pos["id"])   # type: ignore[return-value]

    def _manage(self, pos: dict[str, Any]) -> None:
        """Make the broker's working exit orders match what the position needs."""
        if pos["state"] not in HOLDING_STATES or pos["qty_open"] <= 0 or pos["halted"]:
            return
        if not self.broker.connected or not self.broker.ready(pos["symbol"]):
            return
        now, now_et = self.now(), self.now_et()
        if pos["state"] == INTRADAY and not pos["exit_request"] and self._intraday_over(pos, now_et):
            pos = self._to_swing(pos)

        working = [o for o in self.store.orders_for_position(pos["id"])
                   if o["status"] == O_SUBMITTED and o["role"] != ROLE_ENTRY]
        exits = [o for o in working if o["role"] == ROLE_EXIT]
        prot = [o for o in working if o["role"] in (ROLE_TP, ROLE_SL)]

        if pos["exit_request"]:
            for o in exits:
                if o["type"] == "LMT" and now - o["created_at"] > self.EXIT_LIMIT_TIMEOUT_SEC:
                    self._cancel(o)
            if exits:
                return
            if prot:
                for o in prot:
                    self._cancel(o)
                return
            self._place_exit(pos, now_et)
            return

        want = {ROLE_TP: pos["tp_price"], ROLE_SL: pos["sl_price"]}

        def matches(o: dict[str, Any]) -> bool:
            price = o["limit_price"] if o["role"] == ROLE_TP else o["stop_price"]
            return (o["phase"] == pos["state"] and abs(price - want[o["role"]]) < 1e-6
                    and o["qty"] - int(o["filled"]) == pos["qty_open"])

        roles = [o["role"] for o in prot]
        if all(matches(o) for o in prot) and sorted(roles) == [ROLE_SL, ROLE_TP]:
            self._mismatch_since.pop(pos["id"], None)
            return                                     # fully protected
        if prot:                                       # wrong, partial or duplicated: rebuild
            for o in prot:
                self._cancel(o)
            return
        self._place_protection(pos)

    def _broker_shortfall(self, pos: dict[str, Any]) -> Optional[tuple[float, int]]:
        """(held at broker, expected by the system) when the broker holds fewer shares."""
        held_all = self.broker.positions()
        if held_all is None:
            return None
        held = held_all.get(pos["symbol"], 0.0)
        ours = sum(p["qty_open"] for p in self.store.pos_in_states(HOLDING_STATES)
                   if p["symbol"] == pos["symbol"])
        return (held, ours) if held < ours else None

    def _place_protection(self, pos: dict[str, Any]) -> None:
        sym, st = pos["symbol"], self.settings
        short = self._broker_shortfall(pos)
        if short is not None:
            since = self._mismatch_since.setdefault(pos["id"], self.now())
            if self.now() - since >= self.MISMATCH_ALERT_SEC:
                self.alert(f"אי-התאמה: אצל הברוקר {short[0]:g} מניות, במערכת {short[1]}. פקודות ההגנה לא "
                           f"נשלחות. אם הפוזיציה נסגרה ידנית - סגור אותה גם בדשבורד.", sym, key=f"mismatch:{pos['id']}")
            return
        self._mismatch_since.pop(pos["id"], None)
        if pos["exit_placements"] >= self.MAX_EXIT_PLACEMENTS:
            self.store.pos_update(pos["id"], halted=1)
            self.alert("פקודות ההגנה הוחלפו יותר מדי פעמים - הניהול האוטומטי של הפוזיציה נעצר. "
                       "נדרש טיפול ידני.", sym)
            return
        phase, qty = pos["state"], pos["qty_open"]
        n = pos["exit_placements"] + 1
        oca = f"MLPT-{pos['id']}-{phase}-{n}"
        tp = OrderRequest(symbol=sym, side="SELL", qty=qty, type="LMT", limit_price=pos["tp_price"],
                          tif="GTC", outside_rth=st.tp_outside_rth, oca_group=oca, ref=f"{oca}-TP")
        sl = OrderRequest(symbol=sym, side="SELL", qty=qty, type="STP", stop_price=pos["sl_price"],
                          tif="GTC", outside_rth=st.sl_outside_rth, oca_group=oca, ref=f"{oca}-SL")
        if self._submit_group(pos["id"], [(tp, ROLE_TP), (sl, ROLE_SL)], phase) is not None:
            self.store.pos_update(pos["id"], exit_placements=n)

    def _place_exit(self, pos: dict[str, Any], now_et: dt.datetime) -> None:
        sym, reason = pos["symbol"], pos["exit_request"]
        sess = self.calendar.session(now_et.date())
        phase = sess.phase(now_et) if sess else "CLOSED"
        last = self.last.get(sym)
        if phase == "CLOSED" or (phase != "RTH" and not last):
            self.store.pos_update(pos["id"], exit_request=None)
            self.event("WARN", "ORDER", "לא ניתן לצאת כעת - השוק סגור. ההגנות מוחזרות", sym)
            return
        if phase == "RTH":
            req = OrderRequest(symbol=sym, side="SELL", qty=pos["qty_open"], type="MKT", tif="DAY",
                               ref=f"MLPT-{pos['id']}-EXIT")
        else:   # extended hours accept limit orders only
            limit = round_price(last * (1 - self.EXIT_LIMIT_DISCOUNT_PCT / 100))   # type: ignore[operator]
            req = OrderRequest(symbol=sym, side="SELL", qty=pos["qty_open"], type="LMT", limit_price=limit,
                               tif="DAY", outside_rth=True, ref=f"MLPT-{pos['id']}-EXIT")
        if self._submit(pos["id"], req, ROLE_EXIT, pos["state"], reason) is None:
            self.store.pos_update(pos["id"], exit_request=None)

    # ------------------------------------------------------------------- tick
    def tick(self) -> None:
        """Periodic housekeeping; safe to call as often as once per second."""
        self._safe(self._tick)()

    def _tick(self) -> None:
        st, now, now_et = self.settings, self.now(), self.now_et()
        today = now_et.date()
        today_s = today.isoformat()
        sess = self.calendar.session(today)

        # 1. lists of previous days are never carried over
        for it in self.store.watch_stale(today_s):
            self.store.watch_update(it["id"], status=W_NO_SIGNAL, note="לא התקבל אות ביום המסחר")
        # 2. the entry window is over
        if sess and now_et >= self._at(today, st.entry_end) + dt.timedelta(seconds=self.WINDOW_GRACE_SEC):
            for it in self.store.watch_for_date(today_s):
                if it["status"] == W_WATCHING:
                    self.store.watch_update(it["id"], status=W_NO_SIGNAL, note="לא התקבל אות בחלון הכניסה")
                    self.event("INFO", "SIGNAL", "אין אות בחלון הכניסה - לא נקנתה", it["symbol"])

        if self.broker.connected:
            if self._need_resync or now - self._last_resync >= self.RESYNC_SEC:
                self._resync()
            self._sync_subscriptions(today_s)
            acct = self.broker.account()
            if acct is not None and sess is not None and self.store.kv_get(f"sod:{today_s}") is None:
                self.available_cash(acct, today_s)      # records the start-of-day cash

        positions = self.store.pos_in_states(OPEN_STATES)
        for p in positions:
            if p["state"] == ENTERING:
                # 3. entry orders do not chase the price: cancel what is left
                for o in self.store.orders_for_position(p["id"]):
                    if (o["role"] == ROLE_ENTRY and o["status"] == O_SUBMITTED
                            and now - o["created_at"] >= st.entry_timeout_sec):
                        self._cancel(o)
                continue
            # 4. maximum holding period
            if (p["max_hold_days"] and not p["exit_request"] and sess and sess.is_rth(now_et)
                    and (sess.close - now_et).total_seconds() <= self.TIME_EXIT_BEFORE_CLOSE_SEC
                    and self.calendar.trading_days_between(dt.date.fromisoformat(p["trade_date"]), today)
                    >= p["max_hold_days"]):
                self.store.pos_update(p["id"], exit_request=R_TIME)
                self.event("INFO", "POSITION", f"הגענו למקסימום ימי החזקה ({p['max_hold_days']}) - יציאה", p["symbol"])
                p = self.store.pos_get(p["id"]) or p
            # 5. protective orders
            self._manage(p)

        # 6. working orders of positions that no longer exist
        open_ids = {p["id"] for p in positions}
        for o in self.store.orders_working():
            if o["position_id"] not in open_ids:
                pos = self.store.pos_get(o["position_id"])
                if pos is None or pos["state"] in (CLOSED, CANCELLED):
                    self._cancel(o)

        # 7. deferred sell notifications
        due = [n for n in self._sell_notices if n[0] <= now]
        self._sell_notices = [n for n in self._sell_notices if n[0] > now]
        for _, pos_id in due:
            self._send_sell_notice(pos_id)

    def _sync_subscriptions(self, today_s: str) -> None:
        wanted = {it["symbol"] for it in self.store.watch_for_date(today_s)}
        wanted |= {p["symbol"] for p in self.store.pos_in_states(OPEN_STATES)}
        for sym in sorted(wanted - self.subscribed):
            self._load_series(sym)
            self.subscribed.add(sym)
            self.broker.subscribe(sym)
            self.event("INFO", "DATA", "התחלת איסוף נרות דקה", sym)
        for sym in sorted(self.subscribed - wanted):
            self.subscribed.discard(sym)
            self.broker.unsubscribe(sym)
            self.live.pop(sym, None)

    def _resync(self) -> None:
        """Bring the order book in line with the broker (start-up, reconnect, periodic)."""
        snap = self.broker.snapshot_orders()
        if snap is None:
            return
        self._need_resync = False
        self._last_resync = self.now()
        seen_ids, seen_perm = set(), set()
        for u in snap:
            seen_ids.add(u.broker_order_id)
            if u.perm_id:
                seen_perm.add(str(u.perm_id))
            self.on_order(u)
        for o in self.store.orders_working():
            if o["broker_order_id"] in seen_ids or (o["perm_id"] and o["perm_id"] in seen_perm):
                continue
            if self.now() - o["created_at"] < 30:
                continue                    # just placed, the broker may not list it yet
            self._apply_order(o, O_LOST)

    def on_connection(self, connected: bool, message: str = "") -> None:
        if connected:
            self._need_resync = True
            self.event("INFO", "SYSTEM", f"מחובר לברוקר {message}".strip())
            if self._once_at.pop("disconnect-notified", None) is not None:
                self.notifier.notify("🔌 החיבור לברוקר חזר")
        else:
            self.subscribed.clear()         # the broker re-subscribes; we re-announce
            self.event("ERROR", "SYSTEM", f"החיבור לברוקר נותק {message}".strip())
            if "disconnect-notified" not in self._once_at:
                self._once_at["disconnect-notified"] = self.now()
                self.notifier.notify("⚠️ החיבור לברוקר נותק. הפקודות הפתוחות נשארות פעילות אצל IBKR.")

    def on_broker_error(self, message: str, symbol: Optional[str] = None) -> None:
        if self._once(f"brokererr:{symbol}:{message[:60]}", 300):
            self.event("WARN", "BROKER", message, symbol)

    # ------------------------------------------------------ dashboard actions
    def set_trading(self, enabled: bool) -> None:
        self.settings.trading_enabled = enabled
        self.store.save_settings(self.settings)
        self.event("WARN" if not enabled else "INFO", "SYSTEM",
                   "כניסות חדשות הופעלו" if enabled else "כניסות חדשות הושבתו (פוזיציות פתוחות ממשיכות להיות מנוהלות)")

    def update_settings(self, changes: dict[str, Any]) -> Settings:
        new = Settings.from_dict({**self.settings.to_dict(), **changes})
        new.validate()
        changed = {k: v for k, v in new.to_dict().items() if getattr(self.settings, k) != v}
        self.settings = new
        self.store.save_settings(new)
        if changed:
            self.event("INFO", "SYSTEM", "הגדרות עודכנו: " + ", ".join(f"{k}={v}" for k, v in changed.items()))
        return new

    def suggested_list_date(self) -> dt.date:
        now_et = self.now_et()
        today = now_et.date()
        if self.calendar.is_trading_day(today) and now_et < self._at(today, self.settings.entry_end):
            return today
        return self.calendar.next_trading_day(today)

    def load_watchlist(self, trade_date: str, text: str, replace: bool = False) -> dict[str, Any]:
        try:
            day = dt.date.fromisoformat(trade_date)
        except ValueError:
            return {"ok": False, "errors": [f"תאריך לא תקין: {trade_date}"]}
        now_et = self.now_et()
        if day < now_et.date():
            return {"ok": False, "errors": ["אי אפשר לטעון רשימה לתאריך שעבר"]}
        if not self.calendar.is_trading_day(day):
            return {"ok": False, "errors": [f"{trade_date} אינו יום מסחר"]}
        res = parse_watchlist(text)
        if not res.ok:
            return {"ok": False, "errors": res.errors or ["הרשימה ריקה"]}
        warnings: list[str] = []
        if day == now_et.date() and now_et >= self._at(day, self.settings.entry_end):
            warnings.append("חלון הכניסה של היום כבר נסגר - מניות חדשות לא ייסחרו היום")
        added = updated = kept = removed = 0
        incoming = {r.symbol for r in res.rows}
        for r in res.rows:
            fields = {c: getattr(r, c) for c in PARAM_COLS}
            cur = self.store.watch_find(trade_date, r.symbol)
            if cur is None:
                self.store.watch_add(trade_date, {"symbol": r.symbol, **fields})
                added += 1
            elif cur["status"] == W_WATCHING:
                self.store.watch_update(cur["id"], **fields)
                updated += 1
            else:
                kept += 1
                warnings.append(f"{r.symbol}: כבר טופלה היום ({cur['status']}) - לא עודכנה")
        if replace:
            for cur in self.store.watch_for_date(trade_date):
                if cur["symbol"] not in incoming and cur["status"] == W_WATCHING:
                    self.store.watch_delete(cur["id"])
                    removed += 1
        self.event("INFO", "LIST", f"נטענה רשימה ל-{trade_date}: {added} חדשות, {updated} עודכנו"
                                   f"{f', {removed} הוסרו' if removed else ''}")
        return {"ok": True, "errors": [], "warnings": warnings, "added": added, "updated": updated,
                "kept": kept, "removed": removed}

    def remove_watch(self, watch_id: int) -> tuple[bool, str]:
        it = self.store.watch_get(watch_id)
        if it is None:
            return False, "לא נמצא"
        if it["status"] == W_ENTERED:
            return False, "כבר בוצעה כניסה במניה הזו"
        self.store.watch_delete(watch_id)
        self.event("INFO", "LIST", f"הוסרה מהרשימה של {it['trade_date']}", it["symbol"])
        return True, "הוסרה"

    def request_close(self, pos_id: int) -> tuple[bool, str]:
        """Manual close from the dashboard."""
        pos = self.store.pos_get(pos_id)
        if pos is None or pos["state"] not in HOLDING_STATES:
            return False, "הפוזיציה אינה פתוחה"
        sym = pos["symbol"]
        if not self.broker.connected:
            return False, "אין חיבור לברוקר"
        short = self._broker_shortfall(pos)
        if short is not None and self.now() - (pos["entry_time"] or 0) > 60:
            # the shares are already gone at the broker: just close the books
            price = self.last.get(sym) or pos["entry_price"]
            self.event("WARN", "POSITION", f"נסגרה במערכת בלבד: אצל הברוקר {short[0]:g} מניות. "
                                           f"מחיר היציאה משוער.", sym)
            self._close(pos, price, R_EXTERNAL, pos["state"])
            return True, "הפוזיציה כבר לא קיימת אצל הברוקר - סומנה כסגורה"
        now_et = self.now_et()
        sess = self.calendar.session(now_et.date())
        if sess is None or sess.phase(now_et) == "CLOSED":
            return False, "השוק סגור כעת - אפשר לסגור בין 04:00 ל-20:00 (ניו יורק) בימי מסחר"
        if pos["exit_request"]:
            return False, "כבר קיימת בקשת יציאה"
        self.store.pos_update(pos_id, exit_request=R_MANUAL, halted=0)
        self.event("WARN", "POSITION", "התקבלה בקשת סגירה ידנית", sym)
        self._manage(self.store.pos_get(pos_id))   # type: ignore[arg-type]
        return True, "בקשת הסגירה נשלחה"

    # ---------------------------------------------------------- dashboard data
    def status(self) -> dict[str, Any]:
        st, now_et = self.settings, self.now_et()
        today = now_et.date()
        sess = self.calendar.session(today)
        acct = self.broker.account() if self.broker.connected else None
        day_start = int(dt.datetime.combine(today, dt.time(0), tzinfo=ET).timestamp())
        closed_today = self.store.pos_closed_between(day_start, day_start + 86400)
        open_pos = self.store.pos_in_states(HOLDING_STATES)
        unreal = sum((self.last.get(p["symbol"], p["entry_price"]) - p["entry_price"]) * p["qty_open"]
                     for p in open_pos)
        window_open = bool(sess and self._at(today, st.entry_start) <= now_et < self._at(today, st.entry_end))
        return {
            "mode": self.mode,
            "connected": self.broker.connected,
            "now": int(self.now()),
            "now_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
            "today": today.isoformat(),
            "session": sess.phase(now_et) if sess else "HOLIDAY",
            "entry_window": {"start": st.entry_start, "end": st.entry_end, "open": window_open},
            "trading_enabled": st.trading_enabled,
            "account": ({"id": acct.account, "net_liq": acct.net_liq, "available": acct.available,
                         "for_trading": max(0.0, self.available_cash(acct, today.isoformat()))}
                        if acct else None),
            "open_positions": len(open_pos),
            "unrealized_pnl": round(unreal, 2),
            "realized_today": round(sum(p["pnl"] or 0 for p in closed_today), 2),
            "closed_today": len(closed_today),
            "suggested_list_date": self.suggested_list_date().isoformat(),
        }

    def watchlist_view(self, trade_date: str) -> list[dict[str, Any]]:
        out = []
        for it in self.store.watch_for_date(trade_date):
            row = dict(it)
            row["effective"] = self.params_for(it)
            row["last"] = self.last.get(it["symbol"])
            row["bars"] = len(self.series.get(it["symbol"], []))
            out.append(row)
        return out

    def position_view(self, p: dict[str, Any]) -> dict[str, Any]:
        row = dict(p)
        if p["state"] in HOLDING_STATES:
            last = self.last.get(p["symbol"])
            row["last"] = last
            if last and p["entry_price"]:
                row["unrealized"] = round((last - p["entry_price"]) * p["qty_open"], 2)
                row["unrealized_pct"] = round((last / p["entry_price"] - 1) * 100, 2)
            row["days_held"] = self.calendar.trading_days_between(
                dt.date.fromisoformat(p["trade_date"]), self.now_et().date())
            working = [o for o in self.store.orders_for_position(p["id"]) if o["status"] == O_SUBMITTED]
            row["protected"] = sorted(o["role"] for o in working if o["role"] in (ROLE_TP, ROLE_SL)) == [ROLE_SL, ROLE_TP]
        return row

    def positions_view(self, which: str = "open") -> list[dict[str, Any]]:
        if which == "open":
            rows = self.store.pos_in_states(OPEN_STATES)
        else:
            rows = self.store.pos_closed(500)
        return [self.position_view(p) for p in rows]

    def chart(self, symbol: str, trade_date: str) -> dict[str, Any]:
        """Bars, moving averages and trade markers of one stock for one ET date."""
        st = self.settings
        day = dt.date.fromisoformat(trade_date)
        start = int(dt.datetime.combine(day, dt.time(0), tzinfo=ET).timestamp())
        end = int(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=ET).timestamp())
        warm = self.store.bars(symbol, start - 6 * 86400, start)[-(st.ma_slow * 4 + 50):]
        bars = self.store.bars(symbol, start, end)
        live = self.live.get(symbol)
        if live is not None and start <= live.ts < end and (not bars or live.ts > bars[-1].ts):
            bars = bars + [live]

        def shift(ts: int) -> int:           # chart axis shows New York wall-clock time
            return ts + int(to_et(ts).utcoffset().total_seconds())   # type: ignore[union-attr]

        src = [b for b in warm + bars if b.rth or st.use_extended_bars]
        closes = [b.close for b in src]
        fast = moving_average(closes, st.ma_fast, st.ma_type)
        slow = moving_average(closes, st.ma_slow, st.ma_type)
        ma_fast = [{"time": shift(b.ts), "value": round(v, 4)} for b, v in zip(src, fast)
                   if v is not None and b.ts >= start]
        ma_slow = [{"time": shift(b.ts), "value": round(v, 4)} for b, v in zip(src, slow)
                   if v is not None and b.ts >= start]

        markers, levels = [], []
        for p in self.store.pos_for_symbol(symbol, start, end):
            if start <= p["entry_time"] < end:
                markers.append({"time": shift(p["entry_time"] // 60 * 60), "kind": "entry",
                                "text": f"BUY {p['entry_price']:.2f}"})
            if p["exit_time"] and start <= p["exit_time"] < end:
                markers.append({"time": shift(p["exit_time"] // 60 * 60), "kind": "exit",
                                "win": (p["pnl"] or 0) >= 0,
                                "text": f"SELL {p['exit_price']:.2f} {p['exit_reason']}"})
            if p["state"] in HOLDING_STATES:
                levels = [{"kind": "entry", "price": p["entry_price"]},
                          {"kind": "tp", "price": p["tp_price"]},
                          {"kind": "sl", "price": p["sl_price"]}]
        return {
            "symbol": symbol, "date": trade_date,
            "bars": [{"time": shift(b.ts), "open": b.open, "high": b.high, "low": b.low,
                      "close": b.close, "volume": b.volume, "rth": b.rth} for b in bars],
            "ma_fast": ma_fast, "ma_slow": ma_slow,
            "ma_labels": [f"{st.ma_type}{st.ma_fast}", f"{st.ma_type}{st.ma_slow}"],
            "markers": sorted(markers, key=lambda m: m["time"]), "levels": levels,
        }
