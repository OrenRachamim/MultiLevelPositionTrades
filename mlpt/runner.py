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


def setup_logging(cfg: Config) -> None:
    logs = Path(cfg.data_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file = logging.handlers.RotatingFileHandler(logs / f"mlpt_{cfg.mode}.log", maxBytes=5_000_000,
                                                backupCount=10, encoding="utf-8")
    file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
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


def replay_steps(calendar: MarketCalendar, engine: Engine, sim: SimBroker, source: Store,
                 first: dt.date, days: int) -> Iterator[Step]:
    def day_range(d: dt.date) -> tuple[int, int]:
        return (int(dt.datetime.combine(d, dt.time(0), tzinfo=ET).timestamp()),
                int(dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0), tzinfo=ET).timestamp()))

    start = day_range(first)[0]
    for sym in source.bar_symbols(start - 6 * 86400, start):
        sim.history[sym] = source.bars(sym, start - 6 * 86400, start)
    day = first
    for _ in range(days):
        lo, hi = day_range(day)
        for w in source.watch_for_date(day.isoformat()):
            engine.store.watch_add(day.isoformat(), {"symbol": w["symbol"], **{c: w[c] for c in PARAM_COLS}})
        bars = {sym: source.bars(sym, lo, hi) for sym in source.bar_symbols(lo, hi)}
        if not bars:
            log.warning("no recorded bars for %s", day)
        yield from recorded_day(calendar, day, bars)
        day = calendar.next_trading_day(day)


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
                  overrides: Optional[dict[str, Any]] = None, web: bool = True, seed: int = 7) -> None:
    cfg.mode = "sim"
    setup_logging(cfg)
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
    steps = (demo_steps(calendar, engine, sim, first, days, seed) if kind == "demo"
             else replay_steps(calendar, engine, sim, source, first, days))   # type: ignore[arg-type]

    if not web:
        await _drive(sim, engine, steps, 0)
        print(summary(store))
        return

    async def drive_then_idle() -> None:
        await _drive(sim, engine, steps, speed)
        print(summary(store))
        while True:           # keep the dashboard up for inspection
            await asyncio.sleep(3600)

    await _supervise([asyncio.ensure_future(c) for c in (drive_then_idle(), _serve(engine, cfg))])


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
