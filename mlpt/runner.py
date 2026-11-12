"""Process wiring: builds the engine, the broker and the dashboard and runs them."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import logging.handlers
import random
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from .broker.sim import SimBroker, Step, recorded_day, synthetic_day
from .config import Config, Settings
from .db import Store
from .engine import Engine
from .market_calendar import ET, MarketCalendar, to_et
from .notifier import Notifier, TelegramNotifier
from .watchlist import PARAM_COLS

log = logging.getLogger("mlpt")

DEMO_SYMBOLS = {"AAPL": 228.0, "MSFT": 512.0, "NVDA": 141.0, "AMZN": 214.0, "META": 631.0,
                "GOOGL": 187.0, "TSLA": 342.0, "AMD": 158.0, "NFLX": 97.0, "AVGO": 262.0}


def setup_logging(cfg: Config, quiet: bool = False) -> None:
    logs = Path(cfg.data_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file = logging.handlers.RotatingFileHandler(logs / f"mlpt_{cfg.mode}.log", maxBytes=5_000_000,
                                                backupCount=10, encoding="utf-8")
    file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.ERROR if quiet else logging.INFO)     # the file always gets everything
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [file, console]
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # request URLs contain the bot token


def calendar_from(cfg: Config) -> MarketCalendar:
    return MarketCalendar(tuple(dt.date.fromisoformat(d) for d in cfg.extra_holidays))


def make_notifier(cfg: Config) -> Notifier:
    prefix = "" if cfg.mode == "live" else f"[{cfg.mode.upper()}] "
    if cfg.telegram.bot_token and cfg.telegram.chat_id:
        return TelegramNotifier(cfg.telegram.bot_token, cfg.telegram.chat_id, prefix)
    return Notifier(prefix)


async def _serve(engine: Engine, cfg: Config) -> None:
    import uvicorn

    from .web.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(engine, cfg), host=cfg.web.host, port=cfg.web.port,
                                           log_level="warning", access_log=False))
    log.info("dashboard: http://%s:%s", cfg.web.host, cfg.web.port)
    await server.serve()


async def _supervise(tasks: list[asyncio.Task]) -> None:
    """Run until the first task ends (normally the web server on Ctrl+C)."""
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for t in done:
        if not t.cancelled() and t.exception():
            raise t.exception()   # type: ignore[misc]


# ------------------------------------------------------------------- live ---
async def run_live(cfg: Config) -> None:
    from .broker.ib import IBBroker

    setup_logging(cfg)
    calendar = calendar_from(cfg)
    store = Store(cfg.db_path)
    notifier = make_notifier(cfg)
    broker = IBBroker(cfg.ib, cfg.mode, calendar)
    engine = Engine(store, broker, calendar, notifier, time.time, cfg.mode)
    engine.event("INFO", "SYSTEM", f"המערכת עלתה במצב {cfg.mode.upper()}")
    notifier.notify("🚀 המערכת עלתה")

    async def ticker() -> None:
        while True:
            engine.tick()
            await asyncio.sleep(1.0)

    tasks = [asyncio.ensure_future(c) for c in (broker.run(), ticker(), _serve(engine, cfg))]
    if isinstance(notifier, TelegramNotifier):
        tasks.append(asyncio.ensure_future(notifier.run()))
    try:
        await _supervise(tasks)
    finally:
        engine.event("WARN", "SYSTEM", "המערכת נעצרה (פקודות ההגנה נשארות פעילות אצל IBKR)")
        await broker.stop()
        store.close()


# -------------------------------------------------------------------- sim ---
def _fresh_sim_store(cfg: Config) -> Store:
    for suffix in ("", "-wal", "-shm"):
        Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
    return Store(cfg.db_path)


def demo_steps(calendar: MarketCalendar, engine: Engine, sim: SimBroker, first: dt.date,
               days: int, seed: int) -> Iterator[Step]:
    rng = random.Random(seed)
    prices = dict(DEMO_SYMBOLS)
    # one synthetic day of history so the moving averages are warm at the open
    prev = first - dt.timedelta(days=1)
    while not calendar.is_trading_day(prev):
        prev -= dt.timedelta(days=1)
    for ts, bars in synthetic_day(calendar, prev, prices, rng):
        for sym, bar in bars.items():
            sim.history.setdefault(sym, []).append(bar)
    day, n = first, 0
    while days <= 0 or n < days:
        symbols = rng.sample(sorted(prices), 6)
        engine.load_watchlist(day.isoformat(), "symbol\n" + "\n".join(symbols))
        yield from synthetic_day(calendar, day, prices, rng)
        day, n = calendar.next_trading_day(day), n + 1


def day_range(d: dt.date) -> tuple[int, int]:
    """Epoch range of the New York calendar day ``d``."""
    return (int(dt.datetime.combine(d, dt.time(0), tzinfo=ET).timestamp()),
            int(dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0), tzinfo=ET).timestamp()))


def replay_steps(calendar: MarketCalendar, engine: Engine, sim: SimBroker, source: Store,
                 first: dt.date, days: int, symbols: Optional[list[str]] = None,
                 lists: Optional[dict[str, str]] = None) -> Iterator[Step]:
    """Recorded bars day by day. The daily list comes from ``lists`` (date -> CSV text),
    else ``symbols`` (the same stocks every day), else the lists stored for those days."""
    start = day_range(first)[0]
    for sym in source.bar_symbols(start - 6 * 86400, start):
        sim.history[sym] = source.bars(sym, start - 6 * 86400, start)
    day = first
    for _ in range(days):
        lo, hi = day_range(day)
        if lists is not None:
            if day.isoformat() in lists:
                res = engine.load_watchlist(day.isoformat(), lists[day.isoformat()])
                if not res["ok"]:
                    raise SystemExit(f"list for {day}: {'; '.join(res['errors'])}")
        elif symbols:
            engine.load_watchlist(day.isoformat(), "symbol\n" + "\n".join(symbols))
        else:
            for w in source.watch_for_date(day.isoformat()):
                engine.store.watch_add(day.isoformat(), {"symbol": w["symbol"], **{c: w[c] for c in PARAM_COLS}})
        bars = {sym: source.bars(sym, lo, hi) for sym in source.bar_symbols(lo, hi)}
        if not bars:
            log.warning("no recorded bars for %s", day)
        yield from recorded_day(calendar, day, bars)
        day = calendar.next_trading_day(day)


def read_lists(path: str) -> dict[str, str]:
    """A CSV of historical daily lists (a ``date`` column + the normal list columns) -> date -> CSV text."""
    import csv

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows or "date" not in rows[0] or "symbol" not in rows[0]:
        raise SystemExit(f"{path}: need a header row with at least the columns date,symbol")
    cols = [c for c in rows[0] if c and c != "date"]
    out: dict[str, list[str]] = {}
    for r in rows:
        try:
            day = dt.date.fromisoformat((r["date"] or "").strip()).isoformat()
        except ValueError:
            raise SystemExit(f"{path}: bad date {r['date']!r}, expected YYYY-MM-DD") from None
        out.setdefault(day, [",".join(cols)]).append(",".join((r[c] or "").strip() for c in cols))
    return {day: "\n".join(lines) for day, lines in out.items()}


def stats(store: Store, sim: SimBroker) -> dict[str, Any]:
    """Result of a simulated run, open positions marked at the last price."""
    rows = store._all("SELECT * FROM positions WHERE state != 'CANCELLED' ORDER BY id")
    closed = [r for r in rows if r["state"] == "CLOSED"]
    still = [r for r in rows if r["state"] != "CLOSED"]
    wins = [r["pnl"] for r in closed if r["pnl"] > 0]
    losses = [r["pnl"] for r in closed if r["pnl"] <= 0]
    by: dict[str, list[float]] = {}
    for r in closed:
        by.setdefault(f"{r['exit_phase']} {r['exit_reason']}", []).append(r["pnl"])
    unrealized = sum((sim.last.get(r["symbol"], r["entry_price"]) - r["entry_price"]) * r["qty_open"] for r in still)
    listed = store._all("SELECT status, COUNT(*) n FROM watchlist GROUP BY status")
    realized = sum(r["pnl"] for r in closed)
    return {
        "listed": sum(x["n"] for x in listed), "no_signal": sum(x["n"] for x in listed if x["status"] == "NO_SIGNAL"),
        "skipped": sum(x["n"] for x in listed if x["status"] == "SKIPPED"),
        "trades": len(rows), "closed": len(closed), "wins": len(wins),
        "win_rate": len(wins) / len(closed) * 100 if closed else 0.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "realized": realized, "open": len(still), "unrealized": unrealized, "total": realized + unrealized,
        "commission": sum(r["commission"] or 0 for r in rows),
        "by_exit": {k: (len(v), sum(v)) for k, v in sorted(by.items())},
    }


def trade_rows(store: Store) -> list[dict[str, Any]]:
    out = []
    for r in store._all("SELECT * FROM positions WHERE state != 'CANCELLED' ORDER BY id"):
        out.append({
            "symbol": r["symbol"], "trade_date": r["trade_date"], "qty": r["qty"], "state": r["state"],
            "entry_price": r["entry_price"], "exit_price": r["exit_price"], "pnl": r["pnl"], "pnl_pct": r["pnl_pct"],
            "exit_phase": r["exit_phase"], "exit_reason": r["exit_reason"], "signal": r["signal"],
            "entry_time": to_et(r["entry_time"]).strftime("%H:%M") if r["entry_time"] else None,
            "exit_date": to_et(r["exit_time"]).date().isoformat() if r["exit_time"] else None,
            "exit_time": to_et(r["exit_time"]).strftime("%H:%M") if r["exit_time"] else None,
        })
    return out


def format_stats(s: dict[str, Any]) -> str:
    lines = [
        f"stocks on the lists {s['listed']} | no signal {s['no_signal']} | skipped {s['skipped']} | entries {s['trades']}",
        f"closed {s['closed']} | winners {s['wins']} ({s['win_rate']:.0f}%) | avg win {s['avg_win']:+.2f} | "
        f"avg loss {s['avg_loss']:+.2f}",
    ]
    for k, (n, pnl) in s["by_exit"].items():
        lines.append(f"  {k:16} {n:>4} trades {pnl:>+11.2f}")
    lines.append(f"realized {s['realized']:+.2f} | still open {s['open']} (marked {s['unrealized']:+.2f}) | "
                 f"TOTAL {s['total']:+.2f} after {s['commission']:.2f} commissions")
    return "\n".join(lines)


async def _drive(sim: SimBroker, engine: Engine, steps: Iterator[Step], speed: float) -> None:
    """``speed`` = simulated minutes per real second, 0 = as fast as possible."""
    for n, (ts, bars) in enumerate(steps):
        sim.feed(ts, bars)
        engine.tick()
        await asyncio.sleep(1.0 / speed if speed > 0 else 0)
    engine.tick()
    engine.event("INFO", "SYSTEM", "הסימולציה הסתיימה")


def summary(store: Store) -> str:
    rows = store._all("SELECT * FROM positions WHERE state != 'CANCELLED' ORDER BY id")
    lines = [f"{'symbol':8}{'date':12}{'qty':>6}{'entry':>10}{'exit':>10}{'pnl':>11}  result"]
    for r in rows:
        if r["state"] == "CLOSED":
            lines.append(f"{r['symbol']:8}{r['trade_date']:12}{r['qty']:>6}{r['entry_price']:>10.2f}"
                         f"{r['exit_price']:>10.2f}{r['pnl']:>11.2f}  {r['exit_phase']} {r['exit_reason']}")
        else:
            lines.append(f"{r['symbol']:8}{r['trade_date']:12}{r['qty']:>6}{(r['entry_price'] or 0):>10.2f}"
                         f"{'':>10}{'':>11}  open ({r['state']})")
    closed = [r for r in rows if r["state"] == "CLOSED"]
    wins = sum(1 for r in closed if r["pnl"] > 0)
    lines.append(f"closed {len(closed)} | wins {wins} | realized P&L {sum(r['pnl'] for r in closed):.2f} "
                 f"| still open {len(rows) - len(closed)}")
    return "\n".join(lines)


async def run_sim(cfg: Config, kind: str, *, first: Optional[dt.date] = None, days: int = 1,
                  speed: float = 240.0, cash: float = 100_000.0, source_mode: str = "paper",
                  overrides: Optional[dict[str, Any]] = None, web: bool = True, seed: int = 7,
                  last: Optional[dt.date] = None, symbols: Optional[list[str]] = None,
                  lists: Optional[dict[str, str]] = None, quiet: bool = False,
                  trades: bool = True) -> Optional[dict[str, Any]]:
    cfg.mode = "sim"
    setup_logging(cfg, quiet)
    calendar = calendar_from(cfg)
    source: Optional[Store] = None
    if kind == "replay":
        src_path = Path(cfg.data_dir) / f"mlpt_{source_mode}.db"
        if not src_path.exists():
            raise SystemExit(f"no recorded data: {src_path} does not exist")
        source = Store(src_path)
    store = _fresh_sim_store(cfg)
    settings = source.load_settings() if source else Settings()
    settings = Settings.from_dict({**settings.to_dict(), "trading_enabled": True, **(overrides or {})})
    settings.validate()
    store.save_settings(settings)

    if first is None:
        first = to_et(time.time()).date()
        while not calendar.is_trading_day(first):
            first -= dt.timedelta(days=1)
    elif not calendar.is_trading_day(first):
        raise SystemExit(f"{first} is not a trading day")
    start_ts = calendar.session(first).pre_open.timestamp()   # type: ignore[union-attr]
    sim = SimBroker(calendar, cash=cash, start_ts=start_ts)
    engine = Engine(store, sim, calendar, Notifier("[SIM] "), sim.now, "sim")
    engine.event("INFO", "SYSTEM", "סימולציה עם מחירים אקראיים (Demo)" if kind == "demo"
                 else f"הרצה חוזרת (Replay) של {first} מתוך נתוני {source_mode}")
    if last is not None:
        if last < first:
            raise SystemExit("--to is before --from")
        days = 1 + calendar.trading_days_between(first, last)
    steps = (demo_steps(calendar, engine, sim, first, days, seed) if kind == "demo"
             else replay_steps(calendar, engine, sim, source, first, days, symbols, lists))   # type: ignore[arg-type]

    if not web:
        await _drive(sim, engine, steps, 0)
        result = stats(store, sim)
        if trades:
            print(summary(store))
        print(format_stats(result))
        result["rows"] = trade_rows(store)
        return result

    async def drive_then_idle() -> None:
        await _drive(sim, engine, steps, speed)
        print(summary(store))
        print(format_stats(stats(store, sim)))
        while True:           # keep the dashboard up for inspection
            await asyncio.sleep(3600)

    await _supervise([asyncio.ensure_future(c) for c in (drive_then_idle(), _serve(engine, cfg))])
    return None


async def run_backtest(cfg: Config, *, first: dt.date, last: dt.date, source_mode: str, cash: float,
                       overrides: dict[str, Any], vary: Optional[tuple[str, list[str]]],
                       symbols: Optional[list[str]], lists: Optional[dict[str, str]], web: bool,
                       json_path: str = "", db_name: str = "") -> None:
    """Run the strategy over recorded / downloaded bars; optionally once per value of one setting."""
    import json

    cfg.db_name = db_name
    common: dict[str, Any] = dict(first=first, last=last, source_mode=source_mode, cash=cash,
                                  symbols=symbols, lists=lists)
    if vary is None and not json_path:
        await run_sim(cfg, "replay", overrides=overrides, web=web, speed=0, quiet=True, **common)
        return
    key, values = vary if vary else ("", [""])
    runs = []
    for v in values:
        if key:
            print(f"\n===== {key} = {v} =====", flush=True)
        r = await run_sim(cfg, "replay", overrides={**overrides, key: v} if key else overrides, web=False,
                          quiet=True, trades=not key, **common)
        runs.append({"label": f"{key} = {v}" if key else "backtest", "value": v, **(r or {})})
    if key:
        print(f"\n{key:>22} {'entries':>8} {'closed':>7} {'win %':>6} {'realized':>11} {'open':>5} {'TOTAL':>11}")
        for r in runs:
            print(f"{r['value']:>22} {r['trades']:>8} {r['closed']:>7} {r['win_rate']:>6.0f} "
                  f"{r['realized']:>+11.2f} {r['open']:>5} {r['total']:>+11.2f}")
        print(f"(the database {cfg.db_path} holds the last run)")
    if json_path:
        # which days of the range have no bars at all: "no trades" must not be mistaken for "no data"
        calendar = calendar_from(cfg)
        source = Store(Path(cfg.data_dir) / f"mlpt_{source_mode}.db")
        days, missing, d = 0, [], first
        while d <= last:
            if calendar.is_trading_day(d):
                days += 1
                if not source.bar_symbols(*day_range(d)):
                    missing.append(d.isoformat())
            d += dt.timedelta(days=1)
        source.close()
        payload = {"first": first.isoformat(), "last": last.isoformat(), "vary": key, "overrides": overrides,
                   "cash": cash, "days": days, "missing_days": missing, "runs": runs}
        Path(json_path).write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------- download ---
async def run_download(cfg: Config, symbols: list[str], first: dt.date, last: dt.date, force: bool = False) -> int:
    """Download one-minute history from IB into the database of the configured mode."""
    from .broker.ib import IBBroker

    logging.basicConfig(level=logging.WARNING)
    calendar = calendar_from(cfg)
    store = Store(cfg.db_path)
    days = [d for d in (first + dt.timedelta(days=i) for i in range((last - first).days + 1))
            if calendar.is_trading_day(d)]
    if not symbols:      # default: every stock that was on a daily list in the range
        symbols = sorted({w["symbol"] for d in days for w in store.watch_for_date(d.isoformat())})
    if not symbols or not days:
        print("nothing to download: give --symbols, or load daily lists for these dates first")
        return 1
    cfg.ib.client_id += 1                # do not collide with a running trading process
    broker = IBBroker(cfg.ib, cfg.mode, calendar)
    broker.on_error = lambda msg, sym=None: print(f"  ! {sym or ''} {msg}")
    task = asyncio.ensure_future(broker.run())
    try:
        print(f"connecting to IB Gateway {cfg.ib.host}:{cfg.ib.port} ...")
        for _ in range(60):
            if broker.connected or broker.fatal:
                break
            await asyncio.sleep(0.5)
        if not broker.connected:
            print("FAILED - is IB Gateway running and logged in?")
            return 1
        print(f"{len(symbols)} stocks, {len(days)} trading days ({days[0]} .. {days[-1]}) -> {cfg.db_path}")
        problems = 0
        for sym in symbols:
            have = sum(1 for d in days if sum(1 for b in store.bars(sym, *day_range(d)) if b.rth) >= 200)
            if have == len(days) and not force:
                print(f"  {sym:8} already complete")
                continue
            try:
                bars = await broker.fetch_history(sym, days[0], days[-1])
            except Exception as e:
                print(f"  {sym:8} FAILED: {e}")
                problems += 1
                continue
            for b in bars:
                b.rth = calendar.is_rth_ts(b.ts)
            store.save_bars(sym, bars)
            got = len({to_et(b.ts).date() for b in bars if b.rth})
            print(f"  {sym:8} {len(bars):>7} bars, {got}/{len(days)} days" + ("" if got == len(days) else "  <- incomplete"))
            problems += got < len(days)
        print("done" if not problems else f"done with {problems} incomplete stocks")
        return 0 if not problems else 1
    finally:
        task.cancel()
        await broker.stop()
        store.close()


# ------------------------------------------------------------------ check ---
async def run_check(cfg: Config, symbol: str = "SPY", test_order: bool = False) -> int:
    """Connectivity self-test against IB Gateway. Never trades (see --order)."""
    from .broker.ib import IBBroker
    from .models import O_CANCELLED, O_REJECTED, O_SUBMITTED, OrderRequest

    logging.basicConfig(level=logging.WARNING)
    calendar = calendar_from(cfg)
    broker = IBBroker(cfg.ib, cfg.mode, calendar)
    got: dict[str, Any] = {"history": None, "orders": []}
    broker.on_history = lambda sym, bars: got.__setitem__("history", bars)
    broker.on_error = lambda msg, sym=None: print(f"  ! {sym or ''} {msg}")
    broker.on_order = lambda u: got["orders"].append(u)
    task = asyncio.ensure_future(broker.run())
    ok = True
    try:
        print(f"1. connecting to IB Gateway {cfg.ib.host}:{cfg.ib.port} (client id {cfg.ib.client_id}) ...")
        for _ in range(60):
            if broker.connected or broker.fatal:
                break
            await asyncio.sleep(0.5)
        if not broker.connected:
            print("   FAILED - is IB Gateway running, logged in, and is the API port right?")
            return 1
        print(f"   OK - account {broker.account_id}")
        await asyncio.sleep(2)
        acct = broker.account()
        print(f"2. account data: {'FAILED' if acct is None else 'OK'}")
        if acct:
            print(f"   net liquidation ${acct.net_liq:,.2f} | cash available for trading ${acct.available:,.2f}")
            print(f"   detail: {acct.detail}")
        else:
            ok = False
        print(f"3. positions: {broker.positions()}")
        print(f"4. one-minute bars for {symbol} ...")
        broker.subscribe(symbol)
        for _ in range(60):
            if got["history"] is not None:
                break
            await asyncio.sleep(0.5)
        bars = got["history"]
        if not bars:
            print("   FAILED - no bars (market data subscription? symbol?)")
            ok = False
        else:
            age = time.time() - (bars[-1].ts + 60)
            print(f"   OK - {len(bars)} bars, last completed bar {to_et(bars[-1].ts):%Y-%m-%d %H:%M} ET "
                  f"close {bars[-1].close} ({age / 60:.0f} min ago)")
        if test_order and bars:
            if cfg.mode != "paper":
                print("5. order test skipped: only allowed with mode = \"paper\"")
            else:
                price = round(bars[-1].close * 0.5, 2)
                print(f"5. order test: BUY 1 {symbol} LMT {price} (far from the market), then cancel ...")
                oid = broker.place(OrderRequest(symbol=symbol, side="BUY", qty=1, type="LMT", limit_price=price,
                                                tif="DAY", outside_rth=True, ref="MLPT-CHECK-ENTRY"))
                await asyncio.sleep(3)
                broker.cancel(oid)
                await asyncio.sleep(3)
                statuses = [u.status for u in got["orders"] if u.broker_order_id == oid]
                print(f"   statuses seen: {statuses}")
                good = O_SUBMITTED in statuses and statuses[-1] == O_CANCELLED and O_REJECTED not in statuses
                print(f"   {'OK' if good else 'FAILED - check the messages above'}")
                ok = ok and good
        print("RESULT:", "all good" if ok else "problems found")
        return 0 if ok else 1
    finally:
        task.cancel()
        await broker.stop()
