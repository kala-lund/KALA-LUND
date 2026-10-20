from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
import pytest
from trader.api.server import create_app


def test_api_and_error_json(monkeypatch):
    monkeypatch.setenv("AUTOMATE_ENABLED", "false")
    monkeypatch.setenv("AUTOMATE_DRY_RUN", "true")
    c = TestClient(create_app(), raise_server_exceptions=False)
    h = c.get("/api/health")
    assert h.status_code == 200 and "components" in h.json()
    v = c.get("/api/config/validate")
    assert v.json()["live"] is False and v.json()["trading_mode"] == "paper"
    assert c.get("/api/signals").status_code == 200
    assert c.get("/api/risk").json()["killed"] is False
    r = c.post("/api/orders", json={"symbol": "BTC/USD", "side": "BUY", "notional": 10, "password": "x"})
    assert r.status_code == 403 and r.json()["error"] is True
    r = c.get("/api/jobs/nope")
    assert r.json() == {"error": True, "code": "http_error", "status": 404, "detail": "unknown job"}
    r = c.post("/api/backtest", json={"symbol": "BTC/USD", "limit": "xx"})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    a = c.get("/api/automate/status").json()
    assert a["running"] is False and a["dry_run"] is True
    assert c.post("/api/automate/start", json={"password": "x"}).status_code == 403
    assert c.get("/").status_code == 200


def test_invalid_trading_configuration_fails_startup(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(RuntimeError, match="Invalid trading configuration"):
        create_app()
