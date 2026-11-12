"""SQLite persistence. Everything the system knows survives a restart."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .config import Settings
from .models import Bar

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, rth INTEGER,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
    intraday_tp_pct REAL, intraday_sl_pct REAL, swing_tp_pct REAL, swing_sl_pct REAL,
    max_hold_days INTEGER, position_pct REAL, max_position_usd REAL,
    status TEXT NOT NULL DEFAULT 'WATCHING', note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE (trade_date, symbol)
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, watch_id INTEGER,
    state TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 0,          -- shares bought
    qty_open INTEGER NOT NULL DEFAULT 0,     -- shares still held
    ref_price REAL, entry_price REAL, entry_time INTEGER,
    signal TEXT NOT NULL DEFAULT '',
    intraday_tp_pct REAL, intraday_sl_pct REAL, swing_tp_pct REAL, swing_sl_pct REAL,
    max_hold_days INTEGER NOT NULL DEFAULT 0,
    tp_price REAL, sl_price REAL,            -- levels of the current phase
    swing_time INTEGER,
    exit_request TEXT,                       -- MANUAL / TIME while a forced exit is pending
    exit_price REAL, exit_time INTEGER, exit_reason TEXT, exit_phase TEXT,
    pnl REAL, pnl_pct REAL, commission REAL NOT NULL DEFAULT 0,
    exit_placements INTEGER NOT NULL DEFAULT 0,
    halted INTEGER NOT NULL DEFAULT 0,       -- automatic management stopped
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS positions_state ON positions (state);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL, symbol TEXT NOT NULL,
    role TEXT NOT NULL, phase TEXT, reason TEXT,
    side TEXT NOT NULL, type TEXT NOT NULL, qty INTEGER NOT NULL,
    limit_price REAL, stop_price REAL, tif TEXT, outside_rth INTEGER, oca_group TEXT,
    broker_order_id TEXT, perm_id TEXT,
    status TEXT NOT NULL, filled REAL NOT NULL DEFAULT 0, avg_price REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, cancel_requested_at INTEGER
);
CREATE INDEX IF NOT EXISTS orders_position ON orders (position_id);
CREATE INDEX IF NOT EXISTS orders_broker ON orders (broker_order_id);
CREATE TABLE IF NOT EXISTS fills (
    exec_id TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL, position_id INTEGER NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, ts INTEGER NOT NULL, commission REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL,
    symbol TEXT, message TEXT NOT NULL
);
"""


def _dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.clock = clock          # the engine installs its own clock (simulated time in replay)
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args))]

    def _one(self, sql: str, args: Iterable[Any] = ()) -> Optional[dict[str, Any]]:
        return _dict(self.conn.execute(sql, tuple(args)).fetchone())

    def _update(self, table: str, row_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), row_id))

    def _insert(self, table: str, fields: dict[str, Any]) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)  # type: ignore[arg-type]

    # --- key/value + settings ---------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def load_settings(self) -> Settings:
        d = self.kv_get("settings", {})
        if d and "macd_fast" not in d and d.get("strategy") == "ma_cross":
            # saved before the MACD rule existed: "ma_cross" was only a placeholder default
            d["strategy"] = "macd_cross"
        return Settings.from_dict(d)

    def save_settings(self, s: Settings) -> None:
        self.kv_set("settings", s.to_dict())

    # --- bars ---------------------------------------------------------------
    def save_bar(self, symbol: str, b: Bar) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, b.ts, b.open, b.high, b.low, b.close, b.volume, int(b.rth)),
        )

    def save_bars(self, symbol: str, bars: Iterable[Bar]) -> None:
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(symbol, b.ts, b.open, b.high, b.low, b.close, b.volume, int(b.rth)) for b in bars],
            )
        finally:
            self.conn.execute("COMMIT")

    def bars(self, symbol: str, start_ts: int = 0, end_ts: int = 2**62, limit: int = 0) -> list[Bar]:
        sql = ("SELECT ts, open, high, low, close, volume, rth FROM bars "
               "WHERE symbol = ? AND ts >= ? AND ts < ? ORDER BY ts")
        rows = self.conn.execute(sql, (symbol, start_ts, end_ts)).fetchall()
        if limit:
            rows = rows[-limit:]
        return [Bar(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6])) for r in rows]

    def bar_symbols(self, start_ts: int, end_ts: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT symbol FROM bars WHERE ts >= ? AND ts < ? ORDER BY symbol",
            (start_ts, end_ts)).fetchall()
        return [r[0] for r in rows]

    # --- watchlist ----------------------------------------------------------
    def watch_add(self, trade_date: str, row: dict[str, Any]) -> int:
        return self._insert("watchlist", {"trade_date": trade_date, "created_at": int(self.clock()), **row})

    def watch_for_date(self, trade_date: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM watchlist WHERE trade_date = ? ORDER BY id", (trade_date,))

    def watch_get(self, watch_id: int) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM watchlist WHERE id = ?", (watch_id,))

    def watch_find(self, trade_date: str, symbol: str) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM watchlist WHERE trade_date = ? AND symbol = ?", (trade_date, symbol))

    def watch_stale(self, trade_date: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM watchlist WHERE status = 'WATCHING' AND trade_date < ?", (trade_date,))

    def watch_update(self, watch_id: int, **fields: Any) -> None:
        self._update("watchlist", watch_id, fields)

    def watch_delete(self, watch_id: int) -> None:
        self.conn.execute("DELETE FROM watchlist WHERE id = ?", (watch_id,))

    def watch_dates(self, limit: int = 60) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT trade_date FROM watchlist ORDER BY trade_date DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    # --- positions ----------------------------------------------------------
    def pos_add(self, **fields: Any) -> int:
        return self._insert("positions", {"created_at": int(self.clock()), **fields})

    def pos_get(self, pos_id: int) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM positions WHERE id = ?", (pos_id,))

    def pos_update(self, pos_id: int, **fields: Any) -> None:
        self._update("positions", pos_id, fields)

    def pos_in_states(self, states: Iterable[str]) -> list[dict[str, Any]]:
        states = tuple(states)
        marks = ", ".join("?" for _ in states)
        return self._all(f"SELECT * FROM positions WHERE state IN ({marks}) ORDER BY id", states)

    def pos_for_date(self, trade_date: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM positions WHERE trade_date = ? AND state != 'CANCELLED' ORDER BY id", (trade_date,))

    def pos_closed_between(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM positions WHERE state = 'CLOSED' AND exit_time >= ? AND exit_time < ?",
            (start_ts, end_ts))

    def pos_closed(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM positions WHERE state = 'CLOSED' ORDER BY exit_time DESC, id DESC LIMIT ?", (limit,))

    def pos_for_symbol(self, symbol: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        """Positions of ``symbol`` that were entered or exited in the time range."""
        return self._all(
            "SELECT * FROM positions WHERE symbol = ? AND state != 'CANCELLED' AND entry_time IS NOT NULL "
            "AND entry_time < ? AND COALESCE(exit_time, ?) >= ? ORDER BY id",
            (symbol, end_ts, 2**62, start_ts))

    # --- orders -------------------------------------------------------------
    def order_add(self, **fields: Any) -> int:
        now = int(self.clock())
        return self._insert("orders", {"created_at": now, "updated_at": now, **fields})

    def order_get(self, order_id: int) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM orders WHERE id = ?", (order_id,))

    def order_by_broker_id(self, broker_order_id: str) -> Optional[dict[str, Any]]:
        return self._one(
            "SELECT * FROM orders WHERE broker_order_id = ? ORDER BY id DESC LIMIT 1", (broker_order_id,))

    def order_by_perm_id(self, perm_id: str) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM orders WHERE perm_id = ? ORDER BY id DESC LIMIT 1", (perm_id,))

    def order_update(self, order_id: int, **fields: Any) -> None:
        self._update("orders", order_id, {"updated_at": int(self.clock()), **fields})

    def orders_for_position(self, pos_id: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM orders WHERE position_id = ? ORDER BY id", (pos_id,))

    def orders_working(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM orders WHERE status = 'SUBMITTED' ORDER BY id")

    # --- fills --------------------------------------------------------------
    def fill_upsert(self, exec_id: str, order_id: int, position_id: int, qty: float,
                    price: float, ts: int, commission: Optional[float]) -> None:
        self.conn.execute(
            "INSERT INTO fills (exec_id, order_id, position_id, qty, price, ts, commission) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (exec_id) DO UPDATE SET "
            "commission = COALESCE(excluded.commission, fills.commission)",
            (exec_id, order_id, position_id, qty, price, ts, commission))

    def commission_for_position(self, pos_id: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(commission), 0) FROM fills WHERE position_id = ?", (pos_id,)).fetchone()
        return float(row[0])

    # --- events -------------------------------------------------------------
    def event_add(self, ts: int, level: str, kind: str, message: str, symbol: Optional[str] = None) -> int:
        return self._insert("events", {"ts": ts, "level": level, "kind": kind,
                                       "symbol": symbol, "message": message})

    def events(self, limit: int = 200, after_id: int = 0, symbol: str = "",
               level: str = "") -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE id > ?", [after_id]
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        if level:
            sql += " AND level = ?"
            args.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self._all(sql, args)
