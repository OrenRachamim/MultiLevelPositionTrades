from __future__ import annotations

import datetime as dt

import pytest
from conftest import D1, ts

from mlpt.broker.sim import SimBroker
from mlpt.config import Config, Settings
from mlpt.market_calendar import MarketCalendar
from mlpt.models import (O_CANCELLED, O_FILLED, O_REJECTED, Bar, OrderRequest, round_price)
from mlpt.strategy import Rejected, Signal, ema, ma_cross, ma_trend, macd, macd_cross, sma
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


def test_macd_matches_a_direct_calculation():
    closes = [100 + ((i * 37) % 11) * 0.3 - i * 0.05 for i in range(120)]
    line, sig, hist = macd(closes, 11, 26, 9)
    assert line[:25] == [None] * 25 and line[25] is not None
    assert sig[:33] == [None] * 33 and sig[33] is not None             # 26 + 9 - 1 bars to warm up

    def ema_ref(vals, n):                                               # textbook EMA, SMA-seeded
        out, k = [sum(vals[:n]) / n], 2 / (n + 1)
        for v in vals[n:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    ref_line = [f - s for f, s in zip(ema_ref(closes, 11)[15:], ema_ref(closes, 26))]
    ref_sig = ema_ref(ref_line, 9)
    assert line[-1] == pytest.approx(ref_line[-1]) and sig[-1] == pytest.approx(ref_sig[-1])
    assert hist[-1] == pytest.approx(ref_line[-1] - ref_sig[-1])
    assert all(v == pytest.approx(0) for v in macd([50.0] * 60, 11, 26, 9)[0][25:])


def day_bars(day, closes, start=(9, 30)):
    t0 = ts(day, *start)
    return [Bar(t0 + i * 60, c, c, c, c) for i, c in enumerate(closes)]


def macd_fires(today_closes, prev_closes=None, **settings):
    """Indexes of today's bars on which macd_cross gives a signal."""
    s = Settings(**settings)
    prev = day_bars(dt.date(2026, 11, 9), prev_closes or [100.0] * 90)
    today = day_bars(D1, today_closes)
    return [i for i in range(len(today)) if isinstance(macd_cross(prev + today[:i + 1], s), Signal)]


def test_macd_cross_buys_the_first_upturn_after_a_selloff():
    down = [100 - 0.08 * i for i in range(1, 17)]                      # -1.3% in 16 minutes
    up = [down[-1] + 0.06 * i for i in range(1, 30)]
    fired = macd_fires(down + up)
    assert len(fired) == 1                                             # one cross, one signal
    i = fired[0]
    assert i > len(down)                                               # after the low, on the way up
    closes = [100.0] * 90 + down + up
    line, sig, _ = macd(closes[:90 + i + 1], 11, 26, 9)
    assert line[-1] < 0 and sig[-1] < 0 and line[-1] > sig[-1] and line[-2] <= sig[-2]
    sig_text = macd_cross(day_bars(dt.date(2026, 11, 9), [100.0] * 90) + day_bars(D1, (down + up)[:i + 1]), Settings())
    assert sig_text.price == (down + up)[i] and "MACD" in sig_text.reason


def test_macd_cross_ignores_shallow_dips_and_crosses_above_zero():
    shallow = [100 - 0.004 * i for i in range(1, 11)] + [99.96 + 0.01 * i for i in range(1, 30)]
    assert macd_fires(shallow) == []                                   # below zero, but not "significant"
    assert len(macd_fires(shallow, macd_min_depth_pct=0.0)) == 1       # threshold off: any cross below zero
    # rally, small pullback, rally again: the cross happens above the zero line
    rally = [100 + 0.1 * i for i in range(1, 31)] + [103 - 0.05 * i for i in range(1, 6)] + \
            [102.75 + 0.1 * i for i in range(1, 20)]
    line, sig, _ = macd([100.0] * 90 + rally, 11, 26, 9)
    ups = [i for i in range(91, len(line)) if line[i - 1] <= sig[i - 1] and line[i] > sig[i]]
    assert ups and all(line[i] > 0 for i in ups)                       # the data really has such a cross
    assert macd_fires(rally, macd_min_depth_pct=0.0) == []


def macd_rejections(today_closes, prev_closes=None, **settings):
    s = Settings(**settings)
    prev = day_bars(dt.date(2026, 11, 9), prev_closes or [100.0] * 90)
    today = day_bars(D1, today_closes)
    out = [macd_cross(prev + today[:i + 1], s) for i in range(len(today))]
    return [r.reason for r in out if isinstance(r, Rejected)]


def test_macd_cross_says_why_a_cross_was_not_a_signal():
    shallow = [100 - 0.004 * i for i in range(1, 11)] + [99.96 + 0.01 * i for i in range(1, 30)]
    reasons = macd_rejections(shallow)
    assert len(reasons) == 1 and "עומק" in reasons[0] and "0.1%" in reasons[0]
    rally = [100 + 0.1 * i for i in range(1, 31)] + [103 - 0.05 * i for i in range(1, 6)] + \
            [102.75 + 0.1 * i for i in range(1, 20)]
    assert any("מעל קו האפס" in r for r in macd_rejections(rally))
    prev = [100.0] * 60 + [100 - 0.08 * i for i in range(1, 31)]
    recover = [prev[-1] + 0.05 * i for i in range(1, 40)]
    assert any("יום המסחר הקודם" in r for r in macd_rejections(recover, prev_closes=prev))
    down_up = [100 - 0.08 * i for i in range(1, 17)]
    down_up += [down_up[-1] + 0.06 * i for i in range(1, 30)]
    assert macd_rejections(down_up) == []                               # a clean signal: nothing rejected


def test_macd_cross_needs_the_selloff_to_happen_today():
    # yesterday closed in a sell-off; today only recovers: the cross is not a signal
    prev = [100.0] * 60 + [100 - 0.08 * i for i in range(1, 31)]
    recover = [prev[-1] + 0.05 * i for i in range(1, 40)]
    assert macd_fires(recover, prev_closes=prev) == []
    # ...but a fresh leg down today followed by an upturn is
    again = [prev[-1] + 0.02 * i for i in range(1, 6)] + [prev[-1] + 0.1 - 0.1 * i for i in range(1, 15)]
    again += [again[-1] + 0.06 * i for i in range(1, 30)]
    assert len(macd_fires(again, prev_closes=prev)) == 1


def test_settings_validation_and_coercion():
    s = Settings.from_dict({"position_pct": "12.5", "ma_fast": "5", "tp_outside_rth": "false", "junk": 1})
    assert (s.position_pct, s.ma_fast, s.tp_outside_rth) == (12.5, 5, False)
    s.validate()
    assert (s.strategy, s.macd_fast, s.macd_slow, s.macd_signal) == ("macd_cross", 11, 26, 9)
    for bad in ({"ma_fast": 30}, {"macd_fast": 26}, {"macd_signal": 0}, {"position_pct": 0}, {"position_pct": 150}, {"entry_end": "09:00"},
                {"strategy": "nope"}, {"swing_sl_pct": 100}, {"entry_start": "9h"}):
        with pytest.raises(ValueError):
            Settings.from_dict(bad).validate()
    with pytest.raises(ValueError):
        Settings.from_dict({"ma_fast": "abc"})


def test_settings_saved_before_macd_switch_to_the_macd_rule():
    from mlpt.db import Store
    store = Store(":memory:")
    old = {k: v for k, v in Settings(strategy="ma_cross", position_pct=7.0).to_dict().items()
           if not k.startswith("macd")}
    store.kv_set("settings", old)
    s = store.load_settings()
    assert (s.strategy, s.position_pct, s.macd_fast) == ("macd_cross", 7.0, 11)
    store.save_settings(Settings(strategy="ma_cross"))                     # an explicit choice is kept
    assert store.load_settings().strategy == "ma_cross"


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


# ------------------------------------------------------------------ backtest
def test_historical_lists_file(tmp_path):
    from mlpt.runner import read_lists
    f = tmp_path / "lists.csv"
    f.write_text("\ufeffdate,symbol,intraday_tp_pct\n2026-11-10,AAPL,0.4\n2026-11-10,MSFT,\n2026-11-11,NVDA,0.6\n",
                 encoding="utf-8")
    lists = read_lists(str(f))
    assert sorted(lists) == ["2026-11-10", "2026-11-11"]
    rows = parse_watchlist(lists["2026-11-10"]).rows
    assert [(r.symbol, r.intraday_tp_pct) for r in rows] == [("AAPL", 0.4), ("MSFT", None)]
    bad = tmp_path / "bad.csv"
    bad.write_text("symbol\nAAPL\n")
    with pytest.raises(SystemExit):
        read_lists(str(bad))


def test_backtest_over_stored_bars(tmp_path):
    import asyncio
    import logging
    from mlpt import runner
    from mlpt.db import Store

    src = Store(tmp_path / "mlpt_paper.db")
    for day in (D1, dt.date(2026, 11, 11)):
        bars = day_bars(day, [100.0] * 5 + [100.0, 100.0, 100.3, 100.7, 100.2] + [100.2] * 5, start=(9, 40))
        bars[5].volume = 777                                             # the test strategy's entry signal
        for b in bars:
            b.high, b.low = b.close + 0.05, b.close - 0.05
        src.save_bars("AAPL", bars)
    src.close()
    handlers = logging.getLogger().handlers[:]
    try:
        run = lambda **kw: asyncio.run(runner.run_sim(                  # noqa: E731
            Config(data_dir=str(tmp_path)), "replay", first=D1, last=dt.date(2026, 11, 11), symbols=["AAPL"],
            web=False, quiet=True, trades=False, **kw))
        plain = run(overrides={"strategy": "flag"})
        trail = run(overrides={"strategy": "flag", "intraday_trail_pct": 0.2})
    finally:
        logging.getLogger().handlers[:] = handlers
    assert (plain["listed"], plain["trades"], plain["closed"], plain["wins"]) == (2, 2, 2, 2)
    assert list(plain["by_exit"]) == ["INTRADAY TP"] and plain["by_exit"]["INTRADAY TP"][0] == 2
    assert list(trail["by_exit"]) == ["INTRADAY TRAIL"] and trail["closed"] == 2
    assert plain["total"] == pytest.approx(plain["realized"]) and plain["commission"] == 4.0
    assert "TOTAL" in runner.format_stats(plain)
