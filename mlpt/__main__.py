"""Command line entry point: ``python -m mlpt <command>``."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from typing import Any

from .config import load_config


def _overrides(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"--set expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mlpt", description="Intraday-first / swing-fallback trading over IB Gateway")
    ap.add_argument("-c", "--config", default=None, help="path to config.toml (default: ./config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="trade through IB Gateway (paper or live, per config.toml)")

    ck = sub.add_parser("check", help="test the IB Gateway connection, account data and market data")
    ck.add_argument("--symbol", default="SPY")
    ck.add_argument("--order", action="store_true",
                    help="also place and cancel a far-from-market 1-share order (paper accounts only)")

    dm = sub.add_parser("demo", help="dashboard with a simulated broker and random prices")
    dm.add_argument("--days", type=int, default=0, help="trading days to simulate (0 = endless)")
    dm.add_argument("--speed", type=float, default=30.0, help="simulated minutes per second")
    dm.add_argument("--cash", type=float, default=100_000.0)
    dm.add_argument("--seed", type=int, default=7)
    dm.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting")

    rp = sub.add_parser("replay", help="re-run recorded bars and lists through the strategy")
    rp.add_argument("--date", required=True, help="first trading day, YYYY-MM-DD")
    rp.add_argument("--days", type=int, default=1)
    rp.add_argument("--source", default="paper", choices=("paper", "live"),
                    help="which recorded database to read")
    rp.add_argument("--speed", type=float, default=0.0, help="simulated minutes per second (0 = max)")
    rp.add_argument("--cash", type=float, default=100_000.0)
    rp.add_argument("--no-web", action="store_true", help="print the result and exit")
    rp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting")

    dl = sub.add_parser("download", help="download one-minute history from IB for back-testing")
    dl.add_argument("--symbols", default="", help="comma separated; default: the stocks of the stored daily lists")
    dl.add_argument("--from", dest="first", required=True, help="first day, YYYY-MM-DD")
    dl.add_argument("--to", dest="last", default="", help="last day (default: today)")
    dl.add_argument("--force", action="store_true", help="download again even when the days are already stored")

    bt = sub.add_parser("backtest", help="run the strategy over stored history and print the result")
    bt.add_argument("--from", dest="first", required=True, help="first trading day, YYYY-MM-DD")
    bt.add_argument("--to", dest="last", default="", help="last trading day (default: same as --from)")
    bt.add_argument("--symbols", default="", help="comma separated: trade these stocks on every day")
    bt.add_argument("--lists", default="", help="CSV of historical daily lists: date,symbol,[list columns]")
    bt.add_argument("--source", default="paper", choices=("paper", "live"), help="which database holds the bars")
    bt.add_argument("--cash", type=float, default=100_000.0)
    bt.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting")
    bt.add_argument("--vary", default="", metavar="KEY=V1,V2,...",
                    help="run once per value of one setting and compare, e.g. intraday_trail_pct=0,0.2,0.4")
    bt.add_argument("--web", action="store_true", help="keep the dashboard open on the result")

    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):      # Hebrew log lines on a Windows console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config(args.config)

    from . import runner

    try:
        if args.cmd == "run":
            if cfg.mode == "sim":
                raise SystemExit('mode = "sim" - use "python -m mlpt demo" or "replay" instead')
            asyncio.run(runner.run_live(cfg))
        elif args.cmd == "check":
            return asyncio.run(runner.run_check(cfg, args.symbol.upper(), args.order))
        elif args.cmd == "demo":
            asyncio.run(runner.run_sim(cfg, "demo", days=args.days, speed=args.speed, cash=args.cash,
                                       overrides=_overrides(args.set), seed=args.seed))
        elif args.cmd == "replay":
            asyncio.run(runner.run_sim(cfg, "replay", first=dt.date.fromisoformat(args.date), days=args.days,
                                       speed=args.speed, cash=args.cash, source_mode=args.source,
                                       overrides=_overrides(args.set), web=not args.no_web))
        elif args.cmd == "download":
            last = dt.date.fromisoformat(args.last) if args.last else dt.date.today()
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            return asyncio.run(runner.run_download(cfg, symbols, dt.date.fromisoformat(args.first), last, args.force))
        elif args.cmd == "backtest":
            first = dt.date.fromisoformat(args.first)
            vary = None
            if args.vary:
                if "=" not in args.vary:
                    raise SystemExit("--vary expects KEY=V1,V2,...")
                key, values = args.vary.split("=", 1)
                vary = (key.strip(), [v.strip() for v in values.split(",") if v.strip()])
                if args.web:
                    raise SystemExit("--web cannot be combined with --vary")
            asyncio.run(runner.run_backtest(
                cfg, first=first, last=dt.date.fromisoformat(args.last) if args.last else first,
                source_mode=args.source, cash=args.cash, overrides=_overrides(args.set), vary=vary,
                symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None,
                lists=runner.read_lists(args.lists) if args.lists else None, web=args.web))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
