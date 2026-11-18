from __future__ import annotations

import datetime as dt

import pytest
from conftest import D1, D2, D3, Rig, ts

from mlpt.models import (CANCELLED, CLOSED, INTRADAY, O_SUBMITTED, ROLE_SL, ROLE_TP, SWING, W_ENTERED,
                         W_NO_SIGNAL, W_SKIPPED, W_WATCHING)


def enter(rig: Rig, sym: str = "AAPL", price: float = 100.0, at=(9, 35)) -> int:
    """Signal bar at ``at`` then a fill bar; returns the ts of the next minute."""
    t = ts(D1, *at)
    rig.step(t, sym, price, signal=True)
    rig.step(t + 60, sym, price)
    return t + 120


def roles(rig: Rig) -> list[str]:
    return sorted(o["role"] for o in rig.working())


def test_intraday_take_profit(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    pos = rig.positions()[0]
    assert pos["state"] == INTRADAY and pos["qty"] == 99          # 10% of 100k at limit 100.10
    assert pos["entry_price"] == 100.0
    assert (pos["tp_price"], pos["sl_price"]) == (100.5, 96.0)   # intraday TP, swing stop
    assert roles(rig) == [ROLE_SL, ROLE_TP]
    tp = next(o for o in rig.working() if o["role"] == ROLE_TP)
    sl = next(o for o in rig.working() if o["role"] == ROLE_SL)
    assert tp["tif"] == sl["tif"] == "GTC" and tp["outside_rth"] == 1 and sl["outside_rth"] == 0
    assert tp["oca_group"] == sl["oca_group"]

    rig.step(t, "AAPL", 100.2)
    assert rig.positions()[0]["state"] == INTRADAY
    rig.step(t + 60, "AAPL", 100.6)                               # trades through the take-profit
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "TP" and pos["exit_phase"] == INTRADAY
    assert pos["exit_price"] == 100.5 and pos["pnl"] == 49.5 and pos["pnl_pct"] == 0.5
    assert rig.working() == []                                    # the stop was cancelled (OCA)
    assert rig.sim.pos["AAPL"] == 0
    rig.step(t + 120, "AAPL", 100.6)
    msgs = list(rig.notifier.sent)
    assert any("קנייה AAPL" in m for m in msgs) and any("מכירה AAPL" in m and "+$49.50" in m for m in msgs)


def test_only_one_trade_per_symbol_per_day(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    rig.step(t, "AAPL", 100.6)
    assert rig.positions()[0]["state"] == CLOSED
    rig.step(t + 60, "AAPL", 100.0, signal=True)                  # a second signal is ignored
    rig.step(t + 120, "AAPL", 100.0)
    assert len(rig.positions()) == 1
    assert rig.store.watch_for_date(D1.isoformat())[0]["status"] == W_ENTERED


def test_intraday_stop_loss(rig: Rig):
    rig.load("symbol,intraday_tp_pct,intraday_sl_pct,swing_tp_pct,swing_sl_pct\nAAPL,0.5,2,6,5")
    t = enter(rig)
    assert rig.positions()[0]["sl_price"] == 98.0                 # explicit intraday stop from the list
    rig.step(t, "AAPL", 97.0, open_=99.0)                         # falls through the stop
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "SL" and pos["exit_price"] == 98.0
    assert pos["pnl"] < 0 and rig.working() == []


def test_becomes_swing_and_hits_swing_target(rig: Rig):
    rig.load("symbol\nAAPL")
    enter(rig)
    rig.run("AAPL", D1, (9, 37), [100.1] * 5)
    rig.step(ts(D1, 19, 58), "AAPL", 100.2)                       # still intraday in after-hours
    assert rig.positions()[0]["state"] == INTRADAY
    rig.step(ts(D1, 20, 1))                                       # after-hours are over
    rig.step(ts(D1, 20, 2))
    pos = rig.positions()[0]
    assert pos["state"] == SWING and (pos["tp_price"], pos["sl_price"]) == (105.0, 96.0)
    orders = rig.working()
    assert roles(rig) == [ROLE_SL, ROLE_TP] and all(o["phase"] == SWING for o in orders)
    assert next(o for o in orders if o["role"] == ROLE_TP)["limit_price"] == 105.0
    assert any("עברה לסווינג" in m for m in rig.notifier.sent)

    rig.step(ts(D2, 9, 30), "AAPL", 100.4)                        # the intraday target no longer exits
    assert rig.positions()[0]["state"] == SWING
    rig.step(ts(D2, 10, 0), "AAPL", 105.3)
    rig.step(ts(D2, 10, 1), "AAPL", 105.3)
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "TP" and pos["exit_phase"] == SWING
    assert pos["exit_price"] == 105.0
    assert any("מכירה AAPL · סווינג" in m for m in rig.notifier.sent)


def test_take_profit_fills_after_hours_but_stop_does_not(rig: Rig):
    rig.load("symbol\nAAPL")
    enter(rig)
    rig.step(ts(D1, 17, 0), "AAPL", 95.0)                         # below the stop, outside RTH
    assert rig.positions()[0]["state"] == INTRADAY
    rig.step(ts(D1, 17, 30), "AAPL", 100.7)                       # above the target, outside RTH
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "TP" and pos["exit_phase"] == INTRADAY


def test_no_signal_means_no_trade_and_no_carry_over(rig: Rig):
    rig.load("symbol\nAAPL")
    rig.run("AAPL", D1, (9, 30), [100.0] * 3)
    rig.step(ts(D1, 10, 29), "AAPL", 100.0)
    assert rig.store.watch_for_date(D1.isoformat())[0]["status"] == W_WATCHING
    rig.step(ts(D1, 10, 30), "AAPL", 100.0, signal=True)          # first bar outside the window
    rig.step(ts(D1, 10, 32), "AAPL", 100.0, signal=True)
    assert rig.positions() == []
    assert rig.store.watch_for_date(D1.isoformat())[0]["status"] == W_NO_SIGNAL
    rig.step(ts(D2, 9, 35), "AAPL", 100.0, signal=True)           # not on today's list
    assert rig.positions() == []


def test_last_bar_of_window_can_enter(rig: Rig):
    rig.load("symbol\nAAPL")
    rig.step(ts(D1, 10, 29), "AAPL", 100.0, signal=True)
    rig.step(ts(D1, 10, 30), "AAPL", 100.0)
    assert rig.positions()[0]["state"] == INTRADAY


def test_premarket_signal_ignored_by_default(rig: Rig):
    rig.load("symbol\nAAPL")
    rig.step(ts(D1, 9, 10), "AAPL", 100.0, signal=True)
    assert rig.positions() == []


def test_skip_when_cash_is_short():
    rig = Rig(cash=5_000)
    rig.sim.pos["XYZ"] = 1000                                      # other holdings: net liq ~105k
    rig.sim.last["XYZ"] = 100.0
    rig.load("symbol\nAAPL")
    enter(rig)
    assert rig.positions() == []
    item = rig.store.watch_for_date(D1.isoformat())[0]
    assert item["status"] == W_SKIPPED and "מזומן" in item["note"]
    assert any("דילוג" in m for m in rig.notifier.sent)


def test_cash_spent_today_is_not_reused():
    """Cash account: proceeds of today's sale cannot fund another entry today."""
    rig = Rig(cash=10_000, position_pct=60.0)
    rig.load("symbol\nAAPL\nMSFT")
    t = enter(rig, "AAPL")
    rig.step(t, "AAPL", 100.6)                                    # take-profit: cash is back, unsettled
    assert rig.positions()[0]["state"] == CLOSED
    rig.step(t + 60, "MSFT", 50.0, signal=True)
    rig.step(t + 120, "MSFT", 50.0)
    assert len(rig.positions()) == 1
    assert rig.store.watch_find(D1.isoformat(), "MSFT")["status"] == W_SKIPPED


def test_position_size_cap_from_list(rig: Rig):
    rig.load("symbol,position_pct,max_position_usd\nAAPL,20,2500")
    enter(rig)
    assert rig.positions()[0]["qty"] == 24                         # 2500 // 100.10


def test_unfilled_entry_is_cancelled_and_stock_stays_watched(rig: Rig):
    rig.load("symbol\nAAPL")
    t = ts(D1, 9, 35)
    rig.step(t, "AAPL", 100.0, signal=True)
    rig.step(t + 60, "AAPL", 101.0, open_=100.6)                  # gaps away above the limit
    rig.step(t + 120, "AAPL", 101.0)
    assert rig.positions()[0]["state"] == CANCELLED and rig.working() == []
    assert rig.store.watch_for_date(D1.isoformat())[0]["status"] == W_WATCHING
    rig.step(t + 180, "AAPL", 101.0, signal=True)                 # a later signal may still enter
    rig.step(t + 240, "AAPL", 101.0)
    assert [p["state"] for p in rig.positions()] == [CANCELLED, INTRADAY]


def test_trading_disabled_blocks_entries_only(rig: Rig):
    rig.load("symbol\nAAPL\nMSFT")
    t = enter(rig, "AAPL")
    rig.engine.set_trading(False)
    rig.step(t, "MSFT", 50.0, signal=True)
    rig.step(t + 60, "MSFT", 50.0)
    assert len(rig.positions()) == 1
    rig.step(t + 120, "AAPL", 100.6)                              # open position is still managed
    assert rig.positions()[0]["state"] == CLOSED


def test_existing_position_blocks_second_entry_in_same_symbol(rig: Rig):
    rig.load("symbol\nAAPL")
    enter(rig)
    rig.step(ts(D1, 20, 1))
    rig.load("symbol\nAAPL", D2)
    rig.step(ts(D2, 9, 35), "AAPL", 100.0, signal=True)
    rig.step(ts(D2, 9, 36), "AAPL", 100.0)
    assert len(rig.positions()) == 1
    assert rig.store.watch_for_date(D2.isoformat())[0]["status"] == W_SKIPPED


def test_manual_close_in_regular_hours(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    ok, _ = rig.engine.request_close(rig.positions()[0]["id"])
    assert ok
    rig.step(t, "AAPL", 99.4, open_=99.5)     # protection cancelled, market order fills at the open
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "MANUAL" and pos["exit_price"] == 99.5
    assert rig.working() == []


def test_manual_close_refused_when_market_closed(rig: Rig):
    rig.load("symbol\nAAPL")
    enter(rig)
    rig.step(ts(D1, 21, 0))
    ok, msg = rig.engine.request_close(rig.positions()[0]["id"])
    assert not ok and "סגור" in msg
    assert roles(rig) == [ROLE_SL, ROLE_TP]


def test_max_hold_days_exits_near_the_close(rig: Rig):
    rig.load("symbol,max_hold_days\nAAPL,1")
    enter(rig)
    rig.step(ts(D1, 15, 55), "AAPL", 100.1)                       # entry day does not count
    assert rig.positions()[0]["state"] == INTRADAY
    rig.step(ts(D1, 20, 1))
    rig.step(ts(D2, 15, 45), "AAPL", 101.0)
    assert rig.positions()[0]["state"] == SWING and not rig.positions()[0]["exit_request"]
    rig.step(ts(D2, 15, 51), "AAPL", 101.0)
    rig.step(ts(D2, 15, 52), "AAPL", 101.2)                       # market order fills at the open
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "TIME" and pos["exit_price"] == 101.0


def test_restart_keeps_orders_and_does_not_duplicate(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    before = [o["id"] for o in rig.working()]
    rig.restart()
    rig.step(t, "AAPL", 100.1)
    assert [o["id"] for o in rig.working()] == before
    assert sum(1 for o in rig.sim.orders.values() if o.status == O_SUBMITTED) == 2
    rig.step(t + 60, "AAPL", 100.6)
    assert rig.positions()[0]["state"] == CLOSED


def test_fill_while_engine_was_down_is_picked_up(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    rig.sim.on_order = lambda u: None                              # the engine "process" is dead
    rig.sim.on_execution = lambda e: None
    rig.sim.on_bar = lambda *a: None
    rig.sim.feed(t, {})
    from mlpt.models import Bar
    rig.sim.feed(t + 60, {"AAPL": Bar(t + 60, 100.6, 100.7, 100.5, 100.6, 100)})
    assert rig.positions()[0]["state"] == INTRADAY                # the database missed the fill
    rig.restart()
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "TP"


def test_lost_protection_is_replaced(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    for o in list(rig.sim.orders.values()):                       # e.g. cancelled manually in TWS
        rig.sim.cancel(o.id)
    rig.step(t, "AAPL", 100.1)
    rig.step(t + 60, "AAPL", 100.1)
    assert roles(rig) == [ROLE_SL, ROLE_TP]
    assert sum(1 for o in rig.sim.orders.values() if o.status == O_SUBMITTED) == 2


def test_shares_sold_outside_the_system(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    for o in list(rig.sim.orders.values()):
        rig.sim.cancel(o.id)
    rig.sim.pos["AAPL"] = 0                                        # sold by hand in TWS
    rig.step(t, "AAPL", 100.1)
    rig.step(t + 60, "AAPL", 100.1)
    rig.step(t + 120, "AAPL", 100.1)
    assert rig.working() == []                                     # never sells shares it does not hold
    assert any("אי-התאמה" in m for m in rig.notifier.sent)
    ok, _ = rig.engine.request_close(rig.positions()[0]["id"])
    assert ok
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "EXTERNAL"


def test_runaway_replacement_halts_the_position(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    for i in range(12):
        for o in list(rig.sim.orders.values()):
            rig.sim.cancel(o.id)
        rig.step(t + i * 60, "AAPL", 100.1)
    pos = rig.positions()[0]
    assert pos["halted"] == 1 and pos["exit_placements"] == rig.engine.MAX_EXIT_PLACEMENTS
    assert any("נעצר" in m for m in rig.notifier.sent)


def test_disconnect_pauses_everything(rig: Rig):
    rig.load("symbol\nAAPL\nMSFT")
    t = enter(rig)
    rig.sim.connected = False
    rig.engine.on_connection(False)
    rig.step(t, "MSFT", 50.0, signal=True)
    assert len(rig.positions()) == 1
    rig.sim.connected = True
    rig.engine.on_connection(True)
    rig.step(t + 60, "AAPL", 100.6)
    assert rig.positions()[0]["state"] == CLOSED


def test_list_upload_rules(rig: Rig):
    e = rig.engine
    assert not e.load_watchlist("2026-11-09", "symbol\nAAPL")["ok"]           # past date
    assert not e.load_watchlist("2026-11-14", "symbol\nAAPL")["ok"]           # Saturday
    assert not e.load_watchlist("2026-11-26", "symbol\nAAPL")["ok"]           # Thanksgiving
    assert not e.load_watchlist(D1.isoformat(), "symbol,swing_tp_pct\nAAPL,abc")["ok"]
    assert rig.store.watch_for_date(D1.isoformat()) == []
    assert e.load_watchlist(D1.isoformat(), "symbol\nAAPL\nMSFT")["added"] == 2
    res = e.load_watchlist(D1.isoformat(), "symbol,swing_tp_pct\nAAPL,7\nNVDA,", replace=True)
    assert (res["added"], res["updated"], res["removed"]) == (1, 1, 1)
    assert [w["symbol"] for w in rig.store.watch_for_date(D1.isoformat())] == ["AAPL", "NVDA"]
    assert e.params_for(rig.store.watch_find(D1.isoformat(), "AAPL"))["swing_tp_pct"] == 7
    assert e.params_for(rig.store.watch_find(D1.isoformat(), "NVDA"))["swing_tp_pct"] == 5.0
    assert e.suggested_list_date() == D1


def test_chart_payload(rig: Rig):
    rig.load("symbol\nAAPL")
    t = enter(rig)
    rig.step(t, "AAPL", 100.6)
    c = rig.engine.chart("AAPL", D1.isoformat())
    assert len(c["bars"]) == 3 and [m["kind"] for m in c["markers"]] == ["entry", "exit"]
    # the time axis is shifted to New York wall-clock time
    assert c["bars"][0]["time"] % 86400 == 9 * 3600 + 35 * 60
    assert rig.engine.status()["realized_today"] == 49.5
    assert rig.engine.chart("AAPL", D3.isoformat())["bars"] == []


# ------------------------------------------------------------- bar timing / "why"
def live(rig: Rig, t: int, price: float, volume: float = 777) -> None:
    """A broker update of the minute that is still forming."""
    from mlpt.models import Bar
    rig.engine.on_bar("AAPL", Bar(t, price, price, price, price, volume), False)


def test_minute_is_closed_by_the_clock_without_waiting_for_the_next_bar(rig: Rig):
    from mlpt.models import Bar
    rig.load("symbol\nAAPL")
    t = ts(D1, 9, 45)
    rig.sim.advance(t + 30)
    live(rig, t, 100.0)
    rig.sim.advance(t + 62)
    rig.engine.tick()
    assert rig.positions() == []                                   # minute just ended: wait for late prints
    rig.sim.advance(t + 65)
    rig.engine.tick()
    assert [p["state"] for p in rig.positions()] == ["ENTERING"]   # no trade in 9:46 needed
    assert rig.working()[0]["limit_price"] == 100.1
    # the broker reports the same bar as finished two quiet minutes later: nothing happens twice
    rig.sim.advance(t + 185)
    rig.engine.on_bar("AAPL", Bar(t, 100.0, 100.0, 100.0, 100.0, 777), True)
    rig.engine.tick()
    assert len(rig.positions()) == 1
    assert len(rig.store.bars("AAPL")) == 1


def test_signal_that_arrives_late_is_reported_not_traded(rig: Rig):
    from mlpt.models import Bar
    rig.load("symbol\nAAPL")
    t = ts(D1, 9, 45)
    rig.sim.advance(t + 60 + 120)                                  # the bar shows up two minutes late
    rig.engine.on_bar("AAPL", Bar(t, 100.0, 100.0, 100.0, 100.0, 777), True)
    assert rig.positions() == []
    assert any("באיחור של 120 שניות" in m for m in rig.events("SIGNAL"))
    assert rig.store.watch_for_date(D1.isoformat())[0]["status"] == W_WATCHING


def test_explain_lists_every_trigger_with_its_verdict(rig: Rig):
    from mlpt.models import Bar
    rig.load("symbol\nAAPL")
    rig.step(ts(D1, 9, 31), "AAPL", 100.0)
    rig.sim.feed(ts(D1, 9, 40), {"AAPL": Bar(ts(D1, 9, 40), 100, 100, 100, 100, 555)})    # blocked by a rule
    rig.engine.tick()
    assert any("נר 09:40: אין כניסה - test rule" in m for m in rig.events("SIGNAL"))
    t = enter(rig, at=(9, 45))
    rig.step(t, "AAPL", 100.6)                                     # take-profit
    rig.step(ts(D1, 11, 0), "AAPL", 100.0, signal=True)            # a trigger after the window
    w = rig.engine.explain("AAPL", D1.isoformat())
    assert [(x["hhmm"], x["ok"], x["in_window"]) for x in w["crosses"]] == \
        [("09:40", False, True), ("09:45", True, True), ("11:00", False, False)]
    assert w["crosses"][0]["text"] == "test rule" and "מחוץ לחלון" in w["crosses"][2]["text"]
    assert w["watch"]["status"] == W_ENTERED and w["position"]["qty"] == 99 and w["window"] == ["09:30", "10:30"]
    assert any("אות כניסה" in e["message"] for e in w["events"]) and w["bars_today"] == 6
    empty = rig.engine.explain("MSFT", D1.isoformat())
    assert empty["crosses"] == [] and empty["watch"] is None and empty["position"] is None


# ------------------------------------------------- trailing stop after the target
def trail_rig() -> Rig:
    rig = Rig(intraday_trail_pct=0.2)
    rig.load("symbol\nAAPL")
    return rig


def test_trailing_mode_starts_with_the_stop_only():
    rig = trail_rig()
    t = enter(rig)
    pos = rig.positions()[0]
    assert pos["trail_pct"] == 0.2 and not pos["trail_active"] and pos["tp_price"] == 100.5
    assert roles(rig) == [ROLE_SL] and rig.working()[0]["oca_group"] is None     # no take-profit order
    assert rig.engine.position_view(pos)["protected"] is True
    rig.step(t, "AAPL", 100.3)                                    # below the target: nothing changes
    assert roles(rig) == [ROLE_SL] and not rig.positions()[0]["trail_active"]
    assert any("ואז סטופ נגרר 0.2%" in m for m in rig.notifier.sent)


def test_target_arms_a_trailing_stop_that_follows_the_price_up():
    rig = trail_rig()
    t = enter(rig)
    rig.step(t, "AAPL", 100.6)                                    # trades through the 100.50 target
    pos = rig.positions()[0]
    assert pos["state"] == INTRADAY and pos["trail_active"] == 1 and pos["trail_stop"] == 100.4
    order = rig.working()[0]
    assert roles(rig) == ["TRAIL"] and (order["type"], order["stop_price"], order["trail_pct"]) == ("TRAIL", 100.4, 0.2)
    assert order["tif"] == "GTC" and rig.engine.position_view(pos)["protected"] is True
    assert sum(1 for o in rig.sim.orders.values() if o.status == O_SUBMITTED) == 1     # the stop was replaced
    assert any("🎯 AAPL" in m for m in rig.notifier.sent)

    rig.step(t + 60, "AAPL", 101.0)                               # new high: the stop follows to 100.80
    assert rig.positions()[0]["state"] == INTRADAY
    assert rig.engine.position_view(rig.positions()[0])["trail_stop_now"] == 100.8
    assert [lv["kind"] for lv in rig.engine.chart("AAPL", D1.isoformat())["levels"]] == ["entry", "trail"]
    rig.step(t + 120, "AAPL", 100.5)                              # falls back through it
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "TRAIL" and pos["exit_phase"] == INTRADAY
    assert pos["exit_price"] == 100.8 and pos["pnl"] == 79.2      # more than the 49.50 of a plain take-profit
    assert rig.working() == []
    rig.step(t + 180, "AAPL", 100.5)
    assert any("מכירה AAPL" in m and "סטופ נגרר" in m for m in rig.notifier.sent)


def test_trailing_stop_sells_at_once_when_the_price_falls_straight_back():
    rig = trail_rig()
    t = enter(rig)
    rig.step(t, "AAPL", 100.6)
    rig.step(t + 60, "AAPL", 100.1, open_=100.2)                  # opens below the initial stop of 100.40
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "TRAIL" and pos["exit_price"] == 100.2


def test_trailing_mode_keeps_the_stop_loss_until_the_target():
    rig = trail_rig()
    t = enter(rig)
    rig.step(t, "AAPL", 95.0)
    pos = rig.positions()[0]
    assert pos["state"] == CLOSED and pos["exit_reason"] == "SL" and pos["exit_price"] == 96.0


def test_trailing_is_not_armed_after_hours_or_by_the_fill_bar():
    rig = Rig(intraday_trail_pct=0.2)
    rig.load("symbol\nAAPL")
    t = ts(D1, 9, 35)
    rig.step(t, "AAPL", 100.0, signal=True)
    rig.step(t + 60, "AAPL", 100.0, high=100.9)                   # the high of the fill bar may predate the fill
    assert rig.positions()[0]["state"] == INTRADAY and not rig.positions()[0]["trail_active"]
    rig.step(ts(D1, 17, 0), "AAPL", 100.9)                        # after hours: stops do not work there
    assert not rig.positions()[0]["trail_active"] and roles(rig) == [ROLE_SL]


def test_trailing_position_becomes_a_normal_swing_overnight():
    for armed in (False, True):
        rig = trail_rig()
        t = enter(rig)
        if armed:
            rig.step(t, "AAPL", 100.6)
            rig.step(t + 60, "AAPL", 100.55)
            assert roles(rig) == ["TRAIL"]
        rig.step(ts(D1, 20, 1))
        rig.step(ts(D1, 20, 2))
        pos = rig.positions()[0]
        assert pos["state"] == SWING and (pos["tp_price"], pos["sl_price"]) == (105.0, 96.0)
        assert roles(rig) == [ROLE_SL, ROLE_TP] and all(o["phase"] == SWING for o in rig.working())
        assert sum(1 for o in rig.sim.orders.values() if o.status == O_SUBMITTED) == 2
        rig.step(ts(D2, 10, 0), "AAPL", 105.3)                    # the swing target sells, no trailing
        assert rig.positions()[0]["exit_reason"] == "TP"


def test_restart_keeps_the_trailing_order():
    rig = trail_rig()
    t = enter(rig)
    rig.step(t, "AAPL", 100.6)
    before = [o["id"] for o in rig.working()]
    rig.restart()
    rig.step(t + 60, "AAPL", 100.7)
    assert [o["id"] for o in rig.working()] == before
    assert sum(1 for o in rig.sim.orders.values() if o.status == O_SUBMITTED) == 1
    rig.step(t + 120, "AAPL", 100.2)
    assert rig.positions()[0]["exit_reason"] == "TRAIL"


def test_trailing_setting_applies_to_new_entries_only(rig: Rig):
    rig.load("symbol\nAAPL\nMSFT")
    t = enter(rig, "AAPL")
    rig.engine.update_settings({"intraday_trail_pct": 0.3})
    assert roles(rig) == [ROLE_SL, ROLE_TP]                       # the open position keeps its take-profit
    rig.step(t, "MSFT", 50.0, signal=True)
    rig.step(t + 60, "MSFT", 50.0)
    msft = next(p for p in rig.positions() if p["symbol"] == "MSFT")
    assert msft["trail_pct"] == 0.3
    assert sorted(o["role"] for o in rig.working() if o["symbol"] == "MSFT") == [ROLE_SL]


# ------------------------------------------------------- multi-day / larger candles
def test_chart_over_several_days_in_larger_candles(rig: Rig):
    from mlpt.models import Bar

    def minute_bars(day, hh, mm, n, base, rth=True):
        t0 = ts(day, hh, mm)
        return [Bar(t0 + i * 60, base + i, base + i + 0.5, base + i - 0.5, base + i + 0.2, 10, rth) for i in range(n)]

    rig.store.save_bars("AAPL", minute_bars(D1, 9, 20, 10, 50, rth=False) + minute_bars(D1, 9, 30, 15, 100)
                        + minute_bars(D2, 9, 30, 10, 200))
    one = rig.engine.chart("AAPL", D2.isoformat())
    assert (one["days"], one["tf"], one["first"], len(one["bars"])) == (1, 1, D2.isoformat(), 10)

    c = rig.engine.chart("AAPL", D2.isoformat(), days=2, tf=5)
    assert (c["days"], c["tf"], c["first"]) == (2, 5, D1.isoformat())
    assert [(b["time"] % 86400 // 60, b["rth"]) for b in c["bars"]] == [
        (9 * 60 + 20, False), (9 * 60 + 25, False),                         # pre-market candles stay separate
        (9 * 60 + 30, True), (9 * 60 + 35, True), (9 * 60 + 40, True),      # day 1
        (9 * 60 + 30, True), (9 * 60 + 35, True)]                           # day 2: the overnight gap is skipped
    first_rth = c["bars"][2]
    assert (first_rth["open"], first_rth["high"], first_rth["low"], first_rth["close"], first_rth["volume"]) == \
        (100, 104.5, 99.5, 104.2, 50)                                       # 09:30-09:34 folded into one candle
    hour = rig.engine.chart("AAPL", D1.isoformat(), tf=60)
    assert [(b["time"] % 86400 // 60, b["rth"], b["volume"]) for b in hour["bars"]] == \
        [(8 * 60 + 30, False, 100), (9 * 60 + 30, True, 150)]               # the 09:30 open starts a new candle
    rig.store.save_bars("AAPL", minute_bars(D1, 15, 50, 20, 300, rth=True)[:10] + minute_bars(D1, 16, 0, 10, 310, rth=False))
    late = [b for b in rig.engine.chart("AAPL", D1.isoformat(), tf=60)["bars"] if b["time"] % 86400 >= 15 * 3600]
    assert [(b["time"] % 86400 // 60, b["rth"]) for b in late] == [(15 * 60 + 30, True), (16 * 60, False)]
    stamps = [b["time"] for b in rig.engine.chart("AAPL", D2.isoformat(), days=2, tf=60)["bars"]]
    assert stamps == sorted(set(stamps))                                    # strictly increasing, as the chart needs
    assert rig.engine.chart("AAPL", D2.isoformat(), days=20, tf=1)["tf"] == 5      # long ranges get wider candles
    assert rig.engine.chart("AAPL", D2.isoformat(), days=2, tf=7)["tf"] == 1       # unknown size: one minute


def test_chart_of_a_swing_trade_spans_its_holding_period(rig: Rig):
    rig.load("symbol\nAAPL")
    enter(rig)                                                               # fills in the 09:36 bar of day 1
    rig.step(ts(D1, 20, 1))
    rig.step(ts(D2, 9, 30), "AAPL", 100.4)
    pos = rig.engine.positions_view("open")[0]
    assert pos["state"] == SWING and (pos["chart_date"], pos["chart_days"]) == (D2.isoformat(), 2)
    c = rig.engine.chart("AAPL", pos["chart_date"], days=pos["chart_days"], tf=15)
    assert [m["kind"] for m in c["markers"]] == ["entry"]
    assert c["markers"][0]["time"] == c["bars"][0]["time"]                   # drawn on the candle that contains it
    assert [lv["kind"] for lv in c["levels"]] == ["entry", "tp", "sl"] and c["levels"][1]["price"] == 105.0
    rig.step(ts(D2, 10, 0), "AAPL", 105.3)
    closed = rig.engine.positions_view("closed")[0]
    assert (closed["chart_date"], closed["chart_days"]) == (D2.isoformat(), 2)
    c = rig.engine.chart("AAPL", closed["chart_date"], days=2, tf=15)
    assert [m["kind"] for m in c["markers"]] == ["entry", "exit"] and c["levels"] == []


# ------------------------------------------------------------- comparison index
def test_index_is_chosen_by_correlation_and_added_to_the_chart(rig: Rig):
    import math
    import random
    from mlpt.models import Bar

    rng = random.Random(3)
    price = {"AAPL": 100.0, "SPY": 500.0, "QQQ": 400.0, "DIA": 300.0, "IWM": 200.0}
    bars = {s: [] for s in price}
    for day in (dt.date(2026, 11, 9), D1):
        for i in range(390):
            t = ts(day, 9, 30) + i * 60
            tech, broad = rng.gauss(0, 0.001), rng.gauss(0, 0.001)
            move = {"AAPL": tech + rng.gauss(0, 0.0003), "QQQ": tech + rng.gauss(0, 0.0002),
                    "SPY": broad, "DIA": broad + rng.gauss(0, 0.0003), "IWM": rng.gauss(0, 0.001)}
            for s in price:
                price[s] *= math.exp(move[s])
                bars[s].append(Bar(t, price[s], price[s], price[s], price[s], 100, True))
    for s, series in bars.items():
        rig.store.save_bars(s, series)

    pick = rig.engine.benchmark_for("AAPL", D1)
    assert pick["symbol"] == "QQQ" and pick["by_correlation"]
    corr = {c["symbol"]: c["corr"] for c in pick["candidates"]}
    assert corr["QQQ"] > 0.8 and abs(corr["SPY"]) < 0.3 and all(c["points"] > 100 for c in pick["candidates"])

    c = rig.engine.chart("AAPL", D1.isoformat())
    b = c["benchmark"]
    assert (b["symbol"], b["name"], b["auto"]) == ("QQQ", "Nasdaq 100", True) and b["corr"] == corr["QQQ"]
    assert len(b["points"]) == 390 and b["points"][0]["time"] == c["bars"][0]["time"]
    assert b["points"][-1]["value"] == bars["QQQ"][-1].close                    # raw closes; the page rebases
    hourly = rig.engine.chart("AAPL", D1.isoformat(), days=2, tf=60)["benchmark"]
    assert len(hourly["points"]) == 14                                          # same candles as the stock

    manual = rig.engine.chart("AAPL", D1.isoformat(), bench="DIA")["benchmark"]
    assert (manual["symbol"], manual["auto"], manual["corr"]) == ("DIA", False, corr["DIA"])
    assert rig.engine.chart("AAPL", D1.isoformat(), bench="none")["benchmark"] is None
    assert rig.engine.chart("QQQ", D1.isoformat())["benchmark"] is None         # an index is not compared to itself
    assert rig.engine.chart("AAPL", D1.isoformat(), bench="XYZ")["benchmark"]["symbol"] == "QQQ"

    # too little history: the first configured index, marked as not chosen by correlation
    rig.store.save_bars("NEW", bars["AAPL"][:20])
    weak = rig.engine.chart("NEW", dt.date(2026, 11, 9).isoformat())["benchmark"]
    assert (weak["symbol"], weak["by_correlation"], weak["corr"]) == ("SPY", False, None)
    rig.engine.update_settings({"benchmarks": ""})
    assert rig.engine.chart("AAPL", D1.isoformat())["benchmark"] is None


def test_indexes_are_collected_with_the_listed_stocks_only(rig: Rig):
    assert rig.sim.subscribed == set()                                          # nothing listed: nothing collected
    rig.load("symbol\nAAPL")
    assert rig.sim.subscribed == {"AAPL", "SPY", "QQQ", "DIA", "IWM"}
    rig.step(ts(D1, 9, 35), "SPY", 500.0, signal=True)                          # an index bar is never an entry
    rig.step(ts(D1, 9, 36), "SPY", 500.0)
    assert rig.positions() == [] and len(rig.store.bars("SPY")) == 2
    rig.engine.update_settings({"benchmarks": "QQQ"})
    rig.engine.tick()
    assert rig.sim.subscribed == {"AAPL", "QQQ"}
    with pytest.raises(ValueError):
        rig.engine.update_settings({"benchmarks": "SPY, $$$"})
