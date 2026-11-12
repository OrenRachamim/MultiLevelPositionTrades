"""Parsing and validation of the daily stock list (CSV file or pasted text).

Format - a header row followed by one row per stock. Only ``symbol`` is
mandatory; an empty cell means "use the default from the dashboard settings".

    symbol,intraday_tp_pct,intraday_sl_pct,swing_tp_pct,swing_sl_pct,max_hold_days,position_pct,max_position_usd
    AAPL,0.5,,6,4,5,10,
    NVDA,0.7,3,8,5,,,3000

A bare list of symbols (one per line, or separated by commas / spaces) is also
accepted - every stock then uses the defaults.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}([ .\-][A-Z]{1,2})?$")

FLOAT_COLS = ("intraday_tp_pct", "intraday_sl_pct", "swing_tp_pct", "swing_sl_pct",
              "position_pct", "max_position_usd")
INT_COLS = ("max_hold_days",)
PARAM_COLS = FLOAT_COLS[:4] + INT_COLS + FLOAT_COLS[4:]
COLUMNS = ("symbol",) + PARAM_COLS

# forgiving header aliases
ALIASES = {
    "ticker": "symbol", "stock": "symbol", "סימול": "symbol", "מניה": "symbol",
    "intraday_tp": "intraday_tp_pct", "intraday_sl": "intraday_sl_pct",
    "swing_tp": "swing_tp_pct", "swing_sl": "swing_sl_pct",
    "tp_pct": "swing_tp_pct", "sl_pct": "swing_sl_pct",
    "hold_days": "max_hold_days", "max_days": "max_hold_days",
    "pct": "position_pct", "size_pct": "position_pct",
    "max_usd": "max_position_usd", "max_position": "max_position_usd",
}


@dataclass
class WatchRow:
    symbol: str
    intraday_tp_pct: Optional[float] = None
    intraday_sl_pct: Optional[float] = None
    swing_tp_pct: Optional[float] = None
    swing_sl_pct: Optional[float] = None
    max_hold_days: Optional[int] = None
    position_pct: Optional[float] = None
    max_position_usd: Optional[float] = None


@dataclass
class ParseResult:
    rows: list[WatchRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.rows) and not self.errors


def _norm_header(h: str) -> str:
    h = h.strip().lstrip("﻿").lower().replace(" ", "_").replace("%", "_pct")
    return ALIASES.get(h, h)


def parse_watchlist(text: str) -> ParseResult:
    res = ParseResult()
    text = text.lstrip("﻿").strip()
    if not text:
        res.errors.append("הרשימה ריקה")
        return res
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        res.errors.append("הרשימה ריקה")
        return res

    delim = max(",;\t", key=lines[0].count)
    header = [_norm_header(h) for h in next(csv.reader([lines[0]], delimiter=delim))]

    if "symbol" not in header:
        # no header: treat everything as a bare list of symbols
        tokens = [t for ln in lines for t in re.split(r"[,;\s]+", ln) if t]
        _add_symbols(res, tokens)
        return res

    unknown = [h for h in header if h and h not in COLUMNS]
    if unknown:
        res.errors.append(f"עמודות לא מוכרות: {', '.join(unknown)}")
        return res

    reader = csv.reader(io.StringIO("\n".join(lines[1:])), delimiter=delim)
    seen: set[str] = set()
    for n, cells in enumerate(reader, start=2):
        rec = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header) if h}
        sym = rec.get("symbol", "").upper()
        if not SYMBOL_RE.match(sym):
            res.errors.append(f"שורה {n}: סימול לא תקין {sym!r}")
            continue
        if sym in seen:
            res.errors.append(f"שורה {n}: {sym} מופיע פעמיים")
            continue
        seen.add(sym)
        row = WatchRow(symbol=sym)
        bad = False
        for col in PARAM_COLS:
            raw = rec.get(col, "").replace("%", "").replace("$", "").strip()
            if not raw:
                continue
            try:
                val = float(raw)
            except ValueError:
                res.errors.append(f"שורה {n} ({sym}): ערך לא מספרי בעמודה {col}: {raw!r}")
                bad = True
                continue
            err = _check(col, val)
            if err:
                res.errors.append(f"שורה {n} ({sym}): {err}")
                bad = True
                continue
            setattr(row, col, int(val) if col in INT_COLS else val)
        if not bad:
            res.rows.append(row)
    if not res.rows and not res.errors:
        res.errors.append("לא נמצאו מניות ברשימה")
    return res


def _add_symbols(res: ParseResult, tokens: list[str]) -> None:
    seen: set[str] = set()
    for t in tokens:
        sym = t.upper()
        if not SYMBOL_RE.match(sym):
            res.errors.append(f"סימול לא תקין {t!r} (או שחסרה שורת כותרת עם העמודה symbol)")
        elif sym not in seen:
            seen.add(sym)
            res.rows.append(WatchRow(symbol=sym))


def _check(col: str, v: float) -> Optional[str]:
    if col in ("intraday_tp_pct", "swing_tp_pct") and not 0 < v <= 100:
        return f"{col} חייב להיות בין 0 ל-100"
    if col in ("intraday_sl_pct", "swing_sl_pct") and not 0 < v < 100:
        return f"{col} חייב להיות בין 0 ל-100"
    if col == "position_pct" and not 0 < v <= 100:
        return "position_pct חייב להיות בין 0 ל-100"
    if col == "max_position_usd" and v <= 0:
        return "max_position_usd חייב להיות חיובי"
    if col == "max_hold_days" and (v < 0 or v != int(v)):
        return "max_hold_days חייב להיות מספר שלם לא שלילי"
    return None
