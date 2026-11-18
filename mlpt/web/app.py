"""Dashboard HTTP API. Runs in the same event loop as the engine."""

from __future__ import annotations

import datetime as dt
import secrets
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
import dataclasses
import re

from ..config import Config, Settings
from ..engine import Engine
from ..market_calendar import ET
from ..models import INTRADAY, OPEN_STATES, SWING
from ..notifier import TelegramNotifier
from ..strategy import STRATEGIES
from ..watchlist import SYMBOL_RE
from .jobs import JobRunner

STATIC = Path(__file__).parent / "static"

TEMPLATE = (
    "symbol,intraday_tp_pct,intraday_sl_pct,swing_tp_pct,swing_sl_pct,max_hold_days,position_pct,max_position_usd\n"
    "AAPL,0.5,,6,4,5,10,\n"
    "NVDA,0.7,3,8,5,,,3000\n"
    "MSFT,,,,,,,\n"
)


class WatchlistIn(BaseModel):
    date: str
    text: str
    replace: bool = False


class TradingIn(BaseModel):
    enabled: bool


class BacktestIn(BaseModel):
    first: str
    last: str = ""
    lists_from: str = "stored"          # stored | symbols | file
    symbols: str = ""
    lists_text: str = ""
    overrides: dict[str, Any] = {}
    vary_key: str = ""
    vary_values: str = ""
    cash: float = 100_000.0


class DownloadIn(BaseModel):
    first: str
    last: str = ""
    symbols: str = ""
    force: bool = False


SETTING_KEYS = {f.name for f in dataclasses.fields(Settings)}


def _dates(first: str, last: str) -> tuple[str, str]:
    try:
        a = dt.date.fromisoformat(first)
        b = dt.date.fromisoformat(last) if last else a
    except ValueError:
        raise HTTPException(400, "תאריך לא תקין") from None
    if b < a:
        raise HTTPException(400, "תאריך הסיום לפני תאריך ההתחלה")
    if (b - a).days > 400:
        raise HTTPException(400, "טווח ארוך מדי (עד 400 ימים)")
    return a.isoformat(), b.isoformat()


def _symbols(text: str) -> list[str]:
    out = [s.upper() for s in re.split(r"[,;\s]+", text) if s]
    bad = [s for s in out if not SYMBOL_RE.match(s)]
    if bad:
        raise HTTPException(400, f"סימול לא תקין: {', '.join(bad[:5])}")
    return list(dict.fromkeys(out))


def asset_version() -> str:
    """Changes whenever a dashboard file changes (after an update)."""
    return str(int(max(p.stat().st_mtime for p in STATIC.rglob("*") if p.is_file())))


def create_app(engine: Engine, cfg: Config) -> FastAPI:
    security = HTTPBasic(auto_error=False)

    def auth(creds: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
        if not cfg.web.password:
            return
        ok = creds is not None and secrets.compare_digest(
            creds.username.encode(), cfg.web.username.encode()) and secrets.compare_digest(
            creds.password.encode(), cfg.web.password.encode())
        if not ok:
            raise HTTPException(401, "authentication required", {"WWW-Authenticate": "Basic"})

    app = FastAPI(title="MLPT", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(auth)])

    @app.middleware("http")
    async def guard(request: Request, call_next):   # type: ignore[no-untyped-def]
        # State-changing calls must carry a custom header. Browsers refuse to send
        # it cross-site, so a random web page cannot drive the dashboard.
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-mlpt") != "1":
            return JSONResponse({"detail": "missing X-MLPT header"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index() -> HTMLResponse:
        # every asset URL carries the version, so an updated page never runs a cached old script
        v = asset_version()
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for name in ("style.css", "app.js", "vendor/lightweight-charts.js"):
            html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {**engine.status(), "version": __version__, "assets": asset_version(),
                "telegram": isinstance(engine.notifier, TelegramNotifier),
                "fatal": getattr(engine.broker, "fatal", None)}

    # --- daily list ---------------------------------------------------------
    @app.get("/api/watchlist")
    def watchlist(date: str = "") -> dict[str, Any]:
        date = date or engine.now_et().date().isoformat()
        return {"date": date, "items": engine.watchlist_view(date), "dates": engine.store.watch_dates()}

    @app.post("/api/watchlist")
    def watchlist_upload(body: WatchlistIn) -> dict[str, Any]:
        if len(body.text) > 200_000:
            raise HTTPException(413, "הרשימה גדולה מדי")
        return engine.load_watchlist(body.date, body.text, body.replace)

    @app.delete("/api/watchlist/{watch_id}")
    def watchlist_delete(watch_id: int) -> dict[str, Any]:
        ok, msg = engine.remove_watch(watch_id)
        return {"ok": ok, "message": msg}

    @app.get("/api/watchlist/template")
    def watchlist_template() -> PlainTextResponse:
        return PlainTextResponse(TEMPLATE, media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="watchlist_template.csv"'})

    # --- positions ----------------------------------------------------------
    @app.get("/api/positions")
    def positions(which: str = "open") -> dict[str, Any]:
        if which not in ("open", "closed"):
            raise HTTPException(400, "which must be open or closed")
        rows = engine.positions_view(which)
        out: dict[str, Any] = {"items": rows}
        if which == "closed":
            out["stats"] = _stats(rows)
        return out

    @app.post("/api/positions/{pos_id}/close")
    def position_close(pos_id: int) -> dict[str, Any]:
        ok, msg = engine.request_close(pos_id)
        return {"ok": ok, "message": msg}

    @app.get("/api/positions/{pos_id}/orders")
    def position_orders(pos_id: int) -> dict[str, Any]:
        return {"items": engine.store.orders_for_position(pos_id)}

    # --- charts -------------------------------------------------------------
    @app.get("/api/symbols")
    def symbols(date: str = "") -> dict[str, Any]:
        date = date or engine.now_et().date().isoformat()
        try:
            day = dt.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "bad date") from None
        start = int(dt.datetime.combine(day, dt.time(0), tzinfo=ET).timestamp())
        end = int(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=ET).timestamp())
        with_bars = engine.store.bar_symbols(start, end)
        listed = [w["symbol"] for w in engine.store.watch_for_date(date)]
        held = [p["symbol"] for p in engine.store.pos_in_states(OPEN_STATES)]
        # the comparison indexes are collected for the overlay; they are not stocks to browse
        marks = set(engine.settings.benchmark_list()) - set(held) - set(listed)
        ordered = [s for s in dict.fromkeys(held + listed + with_bars) if s not in marks]
        return {"date": date, "symbols": ordered, "benchmarks": [
            {"symbol": b, "name": engine.BENCHMARK_NAMES.get(b, b)} for b in engine.settings.benchmark_list()]}

    @app.get("/api/chart")
    def chart(symbol: str, date: str = "", days: int = 1, tf: int = 1, bench: str = "auto") -> dict[str, Any]:
        date = date or engine.now_et().date().isoformat()
        try:
            return engine.chart(symbol.upper(), date, days, tf, bench)
        except ValueError:
            raise HTTPException(400, "bad date") from None

    @app.get("/api/explain")
    def explain(symbol: str, date: str = "") -> dict[str, Any]:
        date = date or engine.now_et().date().isoformat()
        try:
            return engine.explain(symbol.upper(), date)
        except ValueError:
            raise HTTPException(400, "bad date") from None

    # --- back-test ----------------------------------------------------------
    jobs = JobRunner(cfg)
    source = cfg.mode          # the database that holds this dashboard's bars and lists

    @app.get("/api/backtest")
    def backtest_status() -> dict[str, Any]:
        return {**jobs.status(), "source": source, "can_download": cfg.mode in ("paper", "live"),
                "settings": engine.settings.to_dict()}

    @app.post("/api/backtest/run")
    def backtest_run(body: BacktestIn) -> dict[str, Any]:
        first, last = _dates(body.first, body.last)
        args = ["backtest", "--from", first, "--to", last, "--source", source, "--cash", str(body.cash),
                "--json", str(jobs.result_path), "--db-name", "backtest"]
        if body.lists_from == "symbols":
            symbols = _symbols(body.symbols)
            if not symbols:
                raise HTTPException(400, "לא הוזנו מניות")
            args += ["--symbols", ",".join(symbols)]
        elif body.lists_from == "file":
            if not body.lists_text.strip():
                raise HTTPException(400, "לא הוזן קובץ רשימות")
            if len(body.lists_text) > 2_000_000:
                raise HTTPException(413, "קובץ הרשימות גדול מדי")
            jobs.dir.mkdir(parents=True, exist_ok=True)
            jobs.lists_path.write_text(body.lists_text, encoding="utf-8")
            args += ["--lists", str(jobs.lists_path)]
        elif body.lists_from != "stored":
            raise HTTPException(400, "lists_from must be stored, symbols or file")
        overrides = {k: v for k, v in body.overrides.items() if str(v).strip() != ""}
        unknown = [k for k in list(overrides) + ([body.vary_key] if body.vary_key else []) if k not in SETTING_KEYS]
        if unknown:
            raise HTTPException(400, f"הגדרה לא מוכרת: {', '.join(unknown)}")
        for k, v in overrides.items():
            args += ["--set", f"{k}={v}"]
        if body.vary_key:
            values = [v.strip() for v in body.vary_values.split(",") if v.strip()]
            if not 1 <= len(values) <= 8:
                raise HTTPException(400, "להשוואה יש להזין בין 1 ל-8 ערכים, מופרדים בפסיקים")
            if any(not re.fullmatch(r"[\w.:\-]+", v) for v in values):
                raise HTTPException(400, "ערך לא תקין להשוואה")
            args += ["--vary", f"{body.vary_key}={','.join(values)}"]
        ok, msg = jobs.start("backtest", args)
        return {"ok": ok, "message": msg}

    @app.post("/api/backtest/download")
    def backtest_download(body: DownloadIn) -> dict[str, Any]:
        if cfg.mode not in ("paper", "live"):
            return {"ok": False, "message": "הורדת נרות זמינה רק כשהמערכת מחוברת ל-IBKR (לא בסימולציה)"}
        first, last = _dates(body.first, body.last)
        args = ["--mode", cfg.mode, "download", "--from", first, "--to", last]
        symbols = _symbols(body.symbols)
        if symbols:
            args += ["--symbols", ",".join(symbols)]
        if body.force:
            args.append("--force")
        ok, msg = jobs.start("download", args)
        return {"ok": ok, "message": msg}

    @app.post("/api/backtest/cancel")
    def backtest_cancel() -> dict[str, Any]:
        ok, msg = jobs.cancel()
        return {"ok": ok, "message": msg}

    # --- log ----------------------------------------------------------------
    @app.get("/api/events")
    def events(limit: int = 200, after_id: int = 0, symbol: str = "", level: str = "") -> dict[str, Any]:
        return {"items": engine.store.events(min(max(limit, 1), 1000), after_id, symbol.upper(), level.upper())}

    # --- settings -----------------------------------------------------------
    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        return {"settings": engine.settings.to_dict(), "strategies": sorted(STRATEGIES)}

    @app.put("/api/settings")
    def settings_update(changes: dict[str, Any]) -> dict[str, Any]:
        try:
            new = engine.update_settings(changes)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"settings": new.to_dict()}

    @app.post("/api/trading")
    def trading(body: TradingIn) -> dict[str, Any]:
        engine.set_trading(body.enabled)
        return {"trading_enabled": engine.settings.trading_enabled}

    @app.post("/api/telegram/test")
    async def telegram_test() -> dict[str, Any]:
        if not isinstance(engine.notifier, TelegramNotifier):
            return {"ok": False, "message": "טלגרם לא מוגדר ב-config.toml"}
        ok, msg = await engine.notifier.test()
        return {"ok": ok, "message": msg}

    return app


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def part(sel: list[dict[str, Any]]) -> dict[str, Any]:
        wins = sum(1 for r in sel if (r["pnl"] or 0) > 0)
        return {"count": len(sel), "wins": wins,
                "win_rate": round(wins / len(sel) * 100, 1) if sel else None,
                "pnl": round(sum(r["pnl"] or 0 for r in sel), 2),
                "commission": round(sum(r["commission"] or 0 for r in sel), 2)}

    return {"all": part(rows),
            "intraday": part([r for r in rows if r["exit_phase"] == INTRADAY]),
            "swing": part([r for r in rows if r["exit_phase"] == SWING])}


__all__ = ["create_app"]
