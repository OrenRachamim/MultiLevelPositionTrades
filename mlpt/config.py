"""Static configuration (config.toml) and runtime settings (stored in the DB).

* ``Config``   - connection details and secrets. Read once at start-up.
* ``Settings`` - trading parameters, editable from the dashboard while running.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODES = ("paper", "live", "sim")


@dataclass
class IBConfig:
    host: str = "127.0.0.1"
    port: int = 4002            # IB Gateway: 4002 paper, 4001 live
    client_id: int = 17
    account: str = ""           # optional; required when the login has several accounts


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    username: str = "admin"
    password: str = ""          # mandatory when host is not a loopback address


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class Config:
    mode: str = "paper"
    data_dir: str = "data"
    extra_holidays: list[str] = field(default_factory=list)
    ib: IBConfig = field(default_factory=IBConfig)
    web: WebConfig = field(default_factory=WebConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    @property
    def db_path(self) -> Path:
        # one database per mode: paper, live and simulation never share state
        return Path(self.data_dir) / f"mlpt_{self.mode}.db"

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.web.host not in ("127.0.0.1", "localhost", "::1") and not self.web.password:
            raise ValueError(
                "web.password is required when the dashboard listens on a non-local "
                "address - the dashboard controls real orders"
            )


def load_config(path: str | os.PathLike | None = None) -> Config:
    raw: dict[str, Any] = {}
    p = Path(path) if path else Path("config.toml")
    if p.exists():
        with p.open("rb") as f:
            raw = tomllib.load(f)
    elif path:
        raise FileNotFoundError(p)
    cfg = Config(
        mode=raw.get("mode", "paper"),
        data_dir=raw.get("data_dir", "data"),
        extra_holidays=list(raw.get("extra_holidays", [])),
        ib=IBConfig(**raw.get("ib", {})),
        web=WebConfig(**raw.get("web", {})),
        telegram=TelegramConfig(**raw.get("telegram", {})),
    )
    # secrets may also come from the environment
    cfg.telegram.bot_token = os.environ.get("MLPT_TELEGRAM_TOKEN", cfg.telegram.bot_token)
    cfg.telegram.chat_id = str(os.environ.get("MLPT_TELEGRAM_CHAT_ID", cfg.telegram.chat_id))
    cfg.web.password = os.environ.get("MLPT_WEB_PASSWORD", cfg.web.password)
    cfg.validate()
    return cfg


# ---------------------------------------------------------------------------


@dataclass
class Settings:
    # master switch for NEW entries (open positions stay protected either way)
    trading_enabled: bool = True

    # sizing
    position_pct: float = 10.0        # % of net liquidation per position
    max_position_usd: float = 0.0     # hard cap per position, 0 = no cap
    cash_reserve_usd: float = 0.0     # cash that is never spent
    max_open_positions: int = 0       # 0 = unlimited

    # default trade parameters (used when the daily list leaves a cell empty)
    intraday_tp_pct: float = 0.5
    # 0 = sell at the intraday target. Above 0: when the price reaches the target the
    # position is not sold; its stop becomes a trailing stop this many percent below the high.
    intraday_trail_pct: float = 0.0
    intraday_sl_pct: float = 0.0      # 0 = use the swing stop
    swing_tp_pct: float = 5.0
    swing_sl_pct: float = 4.0
    max_hold_days: int = 0            # trading days, 0 = no time limit

    # time windows, New York time
    entry_start: str = "09:30"
    entry_end: str = "10:30"
    intraday_end: str = "20:00"       # after this the position becomes a swing

    # execution
    entry_slippage_pct: float = 0.10  # entry limit = signal price + this
    entry_timeout_sec: int = 30
    max_signal_age_sec: int = 30
    tp_outside_rth: bool = True       # take-profit may fill pre/post market
    sl_outside_rth: bool = False

    # intraday entry strategy
    strategy: str = "macd_cross"
    macd_fast: int = 11
    macd_slow: int = 26
    macd_signal: int = 9
    # how far below zero the MACD line must have dropped before the cross,
    # as a percent of the share price (so one value fits every stock); 0 = any
    macd_min_depth_pct: float = 0.10
    ma_type: str = "EMA"
    ma_fast: int = 9
    ma_slow: int = 20
    use_extended_bars: bool = False   # feed pre/post-market bars to the strategy

    def validate(self) -> None:
        def pos(name: str, allow_zero: bool = False) -> None:
            v = getattr(self, name)
            if v < 0 or (v == 0 and not allow_zero):
                raise ValueError(f"{name} must be {'>= 0' if allow_zero else '> 0'}")

        pos("position_pct")
        if self.position_pct > 100:
            raise ValueError("position_pct must be <= 100")
        for n in ("max_position_usd", "cash_reserve_usd", "max_open_positions",
                  "intraday_sl_pct", "max_hold_days", "entry_slippage_pct", "macd_min_depth_pct",
                  "intraday_trail_pct"):
            pos(n, allow_zero=True)
        for n in ("intraday_tp_pct", "swing_tp_pct", "swing_sl_pct",
                  "entry_timeout_sec", "max_signal_age_sec", "ma_fast", "ma_slow",
                  "macd_fast", "macd_slow", "macd_signal"):
            pos(n)
        if self.swing_sl_pct >= 100 or self.intraday_sl_pct >= 100:
            raise ValueError("stop loss must be below 100%")
        if self.intraday_trail_pct >= 20:
            raise ValueError("intraday_trail_pct must be below 20%")
        if self.ma_fast >= self.ma_slow:
            raise ValueError("ma_fast must be smaller than ma_slow")
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be smaller than macd_slow")
        if self.ma_type not in ("EMA", "SMA"):
            raise ValueError("ma_type must be EMA or SMA")
        from .strategy import STRATEGIES  # local import: avoid a cycle
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}")
        times = [parse_hhmm(getattr(self, n)) for n in ("entry_start", "entry_end", "intraday_end")]
        if not times[0] < times[1] <= times[2]:
            raise ValueError("need entry_start < entry_end <= intraday_end")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        """Build from a (possibly partial / stale) dict, coercing types."""
        s = cls()
        for f in dataclasses.fields(cls):
            if f.name not in d or d[f.name] is None:
                continue
            v, default = d[f.name], getattr(s, f.name)
            try:
                if isinstance(default, bool):
                    v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(default, int):
                    v = int(float(v))
                elif isinstance(default, float):
                    v = float(v)
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                raise ValueError(f"bad value for {f.name}: {d[f.name]!r}") from None
            setattr(s, f.name, v)
        return s


def parse_hhmm(s: str) -> dt.time:
    try:
        h, m = s.strip().split(":")
        return dt.time(int(h), int(m))
    except Exception:
        raise ValueError(f"bad time {s!r}, expected HH:MM") from None
