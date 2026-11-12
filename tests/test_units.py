from __future__ import annotations

import datetime as dt

import pytest
from conftest import D1, ts

from mlpt.broker.sim import SimBroker
from mlpt.config import Config, Settings
from mlpt.market_calendar import MarketCalendar
from mlpt.models import (O_CANCELLED, O_FILLED, O_REJECTED, Bar, OrderRequest, round_price)
from mlpt.strategy import ema, ma_cross, ma_trend, sma
from mlpt.watchlist import parse_watchlist


# ---------------------------------------------------------------- watchlist
def test_parse_full_csv():
    res = parse_watchlist(
        "symbol,intraday_tp_pct,intraday_sl_pct,swing_tp_pct,swing_sl_pct,max_hold_days,position_pct,max_position_usd\n"
        "aapl,0.5,,6,4,5,10,\n"
        "NVDA,0.7%,3,8,5,,,$3000\n")
    assert res.ok and [r.symbol for r in res.rows] == ["AAPL", "NVDA"]
    a, n = res.rows
    assert (a.intraday_tp_pct, a.intraday_sl_pct, a.swing_tp_pct, a.max_hold_days) == (0.5, None, 6, 5)
    assert (n.intraday_tp_pct, n.position_pct, n.max_position_usd) == (0.7, None, 3000)


def test_parse_bare_symbols_and_other_delimiters():
    assert [r.symbol for r in parse_watchlist("AAPL, msft\nNVDA  BRK.B").rows] == ["AAPL", "MSFT", "NVDA", "BRK.B"]
    res = parse_watchlist("﻿Symbol;Swing TP;Swing SL\nAAPL;6;4\n")      # Excel export: BOM + semicolons
    assert res.ok and res.rows[0].swing_tp_pct == 6 and res.rows[0].swing_sl_pct == 4
    assert parse_watchlist("ticker\tsl_pct\nAAPL\t3").rows[0].swing_sl_pct == 3


@pytest.mark.parametrize("text", [
    "", "symbol\n", "symbol,foo\nAAPL,1", "symbol,swing_tp_pct\nAAPL,x", "symbol,swing_sl_pct\nAAPL,150",
    "symbol\nAAPL\nAAPL", "symbol\n123", "symbol,position_pct\nAAPL,0", "symbol,max_hold_days\nAAPL,1.5",
    "AAPL, $$$",
])
def test_parse_rejects_bad_input(text):
    res = parse_watchlist(text)
    assert not res.ok and res.errors


# ----------------------------------------------------------------- calendar
def test_calendar_holidays_and_sessions():
    cal = MarketCalendar()
    closed = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
              "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2026-11-14"]
    assert not any(cal.is_trading_day(dt.date.fromisoformat(d)) for d in closed)
    assert cal.is_trading_day(dt.date(2026, 11, 11))                       # Veterans Day: open
    assert cal.is_trading_day(dt.date(2021, 12, 31))                       # New Year on Saturday: Friday open
    assert cal.session(dt.date(2026, 11, 27)).close.hour == 13             # day after Thanksgiving
    assert cal.session(dt.date(2026, 12, 24)).close.hour == 13
    assert cal.next_trading_day(dt.date(2026, 11, 25)) == dt.date(2026, 11, 27)
    assert cal.trading_days_between(dt.date(2026, 11, 25), dt.date(2026, 11, 30)) == 2
    s = cal.session(D1)
    phases = [s.phase(dt.datetime.fromtimestamp(ts(D1, h, m), s.open.tzinfo))
              for h, m in [(3, 59), (4, 0), (9, 29), (9, 30), (15, 59), (16, 0), (19, 59), (20, 0)]]
    assert phases == ["CLOSED", "PRE", "PRE", "RTH", "RTH", "POST", "POST", "CLOSED"]
    assert cal.is_rth_ts(ts(D1, 9, 30)) and not cal.is_rth_ts(ts(D1, 16, 0))
    assert not MarketCalendar((D1,)).is_trading_day(D1)


def test_dst_mismatch_week_uses_new_york_time():
    # 2026-03-10: the US is already on summer time, Israel is not - the open is 13:30 UTC
    s = MarketCalendar().session(dt.date(2026, 3, 10))
    assert s.open.astimezone(dt.timezone.utc).hour == 13 and s.open.astimezone(dt.timezone.utc).minute == 30


# ----------------------------------------------------------------- strategy
def bars_from(closes):
    return [Bar(i * 60, c, c, c, c) for i, c in enumerate(closes)]


def test_moving_averages():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2.0 and e[3] == 3.0 and e[4] == 4.0


def test_ma_cross_fires_once_on_the_cross():
    s = Settings(ma_fast=3, ma_slow=6)
    closes = [10.0] * 8 + [9.8, 9.6, 9.5, 9.5, 9.6, 9.9, 10.3, 10.6, 10.8]
    fired = [i for i in range(1, len(closes) + 1) if ma_cross(bars_from(closes[:i]), s)]
    assert len(fired) == 1 and closes[fired[0] - 1] > 9.6
    assert ma_cross(bars_from([10.0] * 5), s) is None                      # not enough bars
    assert ma_trend(bars_from(closes), s) is None                          # flat bars: close == open


def test_settings_validation_and_coercion():
    s = Settings.from_dict({"position_pct": "12.5", "ma_fast": "5", "tp_outside_rth": "false", "junk": 1})
    assert (s.position_pct, s.ma_fast, s.tp_outside_rth) == (12.5, 5, False)
    s.validate()
    for bad in ({"ma_fast": 30}, {"position_pct": 0}, {"position_pct": 150}, {"entry_end": "09:00"},
                {"strategy": "nope"}, {"swing_sl_pct": 100}, {"entry_start": "9h"}):
        with pytest.raises(ValueError):
            Settings.from_dict(bad).validate()
    with pytest.raises(ValueError):
        Settings.from_dict({"ma_fast": "abc"})


def test_config_requires_password_when_exposed():
    cfg = Config()
    cfg.web.host = "0.0.0.0"
    with pytest.raises(ValueError):
        cfg.validate()
    cfg.web.password = "x"
    cfg.validate()
    assert Config(mode="live").db_path.name == "mlpt_live.db"


def test_round_price():
    assert round_price(123.456) == 123.46 and round_price(0.12345) == 0.1235


# ---------------------------------------------------------------- simulator
def test_sim_fill_rules():
    cal = MarketCalendar()
    sim = SimBroker(cal, cash=10_000, start_ts=ts(D1, 9, 30), commissions=True)
    seen = {}
    sim.on_order = lambda u: seen.__setitem__(u.broker_order_id, u)
    buy = sim.place(OrderRequest("AAPL", "BUY", 10, "LMT", limit_price=100.0))
    sim.feed(ts(D1, 9, 30), {"AAPL": Bar(ts(D1, 9, 30), 101, 102, 100.5, 101)})
    assert seen[buy].status != O_FILLED                                    # never traded at the limit
    sim.feed(ts(D1, 9, 31), {"AAPL": Bar(ts(D1, 9, 31), 99.5, 101, 99, 100)})
    assert seen[buy].status == O_FILLED and seen[buy].avg_price == 99.5    # better open: filled at the open
    assert sim.cash == 10_000 - 995 - 1.0                                  # $1 minimum commission

    tp = sim.place(OrderRequest("AAPL", "SELL", 10, "LMT", limit_price=101.0, tif="GTC", oca_group="g"))
    sl = sim.place(OrderRequest("AAPL", "SELL", 10, "STP", stop_price=98.0, tif="GTC", oca_group="g"))
    sim.feed(ts(D1, 9, 32), {"AAPL": Bar(ts(D1, 9, 32), 100, 101.5, 97.5, 99)})     # touches both
    assert seen[sl].status == O_FILLED and seen[sl].avg_price == 98.0      # the stop wins
    assert seen[tp].status == O_CANCELLED and sim.pos["AAPL"] == 0

    # proceeds are not available until the next trading day (cash account)
    assert sim.account().available == pytest.approx(10_000 - 995 - 1.0)
    big = sim.place(OrderRequest("AAPL", "BUY", 95, "LMT", limit_price=100.0))
    sim.feed(ts(D1, 9, 33), {"AAPL": Bar(ts(D1, 9, 33), 99, 99, 99, 99)})
    assert seen[big].status == O_REJECTED
    short = sim.place(OrderRequest("AAPL", "SELL", 5, "MKT"))
    sim.feed(ts(D1, 9, 34), {"AAPL": Bar(ts(D1, 9, 34), 99, 99, 99, 99)})
    assert seen[short].status == O_REJECTED                                # no short selling

    day = sim.place(OrderRequest("AAPL", "BUY", 1, "LMT", limit_price=50.0, tif="DAY"))
    sim.advance(ts(D1, 16, 1))
    assert seen[day].status == O_CANCELLED                                 # DAY order expired at the close
    sim.advance(ts(dt.date(2026, 11, 11), 9, 0))
    assert sim.account().available == pytest.approx(sim.cash)              # settled the next day
