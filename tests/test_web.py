from __future__ import annotations

from conftest import D1, Rig, ts
from fastapi.testclient import TestClient

from mlpt.config import Config
from mlpt.web.app import create_app

H = {"X-MLPT": "1"}


def client(rig: Rig, password: str = "") -> TestClient:
    cfg = Config(mode="sim")
    cfg.web.password = password
    return TestClient(create_app(rig.engine, cfg))


def test_dashboard_flow(rig: Rig):
    c = client(rig)
    assert c.get("/").status_code == 200 and "MLPT" in c.get("/").text
    assert c.get("/static/app.js").status_code == 200
    st = c.get("/api/status").json()
    assert st["mode"] == "sim" and st["connected"] and st["suggested_list_date"] == D1.isoformat()

    bad = c.post("/api/watchlist", json={"date": D1.isoformat(), "text": "symbol,x\nAAPL,1"}, headers=H).json()
    assert not bad["ok"] and bad["errors"]
    ok = c.post("/api/watchlist", json={"date": D1.isoformat(), "text": "symbol,swing_tp_pct\nAAPL,7\nMSFT,"},
                headers=H).json()
    assert ok["ok"] and ok["added"] == 2
    items = c.get(f"/api/watchlist?date={D1}").json()["items"]
    assert items[0]["effective"]["swing_tp_pct"] == 7 and items[1]["effective"]["swing_tp_pct"] == 5.0
    assert c.delete(f"/api/watchlist/{items[1]['id']}", headers=H).json()["ok"]

    rig.engine.tick()
    rig.step(ts(D1, 9, 35), "AAPL", 100.0, signal=True)
    rig.step(ts(D1, 9, 36), "AAPL", 100.0)
    pos = c.get("/api/positions").json()["items"]
    assert len(pos) == 1 and pos[0]["protected"] and pos[0]["unrealized"] == 0
    assert len(c.get(f"/api/positions/{pos[0]['id']}/orders").json()["items"]) == 3
    assert c.get(f"/api/symbols?date={D1}").json()["symbols"] == ["AAPL"]
    chart = c.get(f"/api/chart?symbol=aapl&date={D1}").json()
    assert len(chart["bars"]) == 2 and chart["markers"][0]["kind"] == "entry" and len(chart["levels"]) == 3

    assert c.post(f"/api/positions/{pos[0]['id']}/close", headers=H).json()["ok"]
    rig.step(ts(D1, 9, 37), "AAPL", 100.2)
    closed = c.get("/api/positions?which=closed").json()
    assert closed["items"][0]["exit_reason"] == "MANUAL" and closed["stats"]["all"]["count"] == 1
    assert c.get("/api/events?level=warn").json()["items"]
    assert c.get("/api/watchlist/template").text.startswith("symbol,")


def test_settings_and_kill_switch(rig: Rig):
    c = client(rig)
    assert "flag" in c.get("/api/settings").json()["strategies"]
    r = c.put("/api/settings", json={"position_pct": "15", "ma_fast": 5, "tp_outside_rth": False}, headers=H)
    assert r.status_code == 200 and r.json()["settings"]["position_pct"] == 15
    assert rig.store.load_settings().tp_outside_rth is False               # persisted
    assert c.put("/api/settings", json={"ma_fast": 50}, headers=H).status_code == 400
    assert rig.engine.settings.ma_fast == 5                                # a rejected change changes nothing
    assert c.post("/api/trading", json={"enabled": False}, headers=H).json() == {"trading_enabled": False}
    assert c.get("/api/status").json()["trading_enabled"] is False
    assert c.post("/api/telegram/test", headers=H).json()["ok"] is False   # not configured


def test_state_changes_need_the_custom_header(rig: Rig):
    c = client(rig)
    assert c.post("/api/trading", json={"enabled": False}).status_code == 403
    assert c.put("/api/settings", json={"position_pct": 50}).status_code == 403
    assert rig.engine.settings.trading_enabled and rig.engine.settings.position_pct == 10.0


def test_password_protection(rig: Rig):
    c = client(rig, password="s3cret")
    assert c.get("/api/status").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/api/status", auth=("admin", "wrong")).status_code == 401
    assert c.get("/api/status", auth=("admin", "s3cret")).status_code == 200


def test_assets_are_versioned_so_updates_are_never_half_loaded(rig: Rig):
    c = client(rig)
    page = c.get("/")
    version = c.get("/api/status").json()["assets"]
    assert page.headers["cache-control"] == "no-store"
    for name in ("style.css", "app.js", "vendor/lightweight-charts.js"):
        assert f'"/static/{name}?v={version}"' in page.text
        assert c.get(f"/static/{name}?v={version}").status_code == 200


def test_chart_reports_why_macd_is_empty(rig: Rig):
    c = client(rig)
    c.post("/api/watchlist", json={"date": D1.isoformat(), "text": "AAPL"}, headers=H)
    rig.engine.tick()
    rig.step(ts(D1, 8, 0), "AAPL", 100.0)                                  # pre-market bars only
    rig.step(ts(D1, 8, 1), "AAPL", 100.1)
    chart = c.get(f"/api/chart?symbol=AAPL&date={D1}").json()
    assert len(chart["bars"]) == 2 and chart["macd"] == [] and chart["indicator_bars"] == 0
    assert chart["macd_need"] == 34 and chart["uses_extended"] is False
    rig.run("AAPL", D1, (9, 30), [100.0 + 0.01 * i for i in range(40)])
    chart = c.get(f"/api/chart?symbol=AAPL&date={D1}").json()
    assert chart["indicator_bars"] == 40 and len(chart["macd"]) == 15      # line from bar 26
    assert len(chart["macd_signal"]) == 7 and len(chart["macd_hist"]) == 7  # signal from bar 34
