from __future__ import annotations

"""Contract tests for the dashboard-facing V2 API.

Hermetic: conftest.py blanks DATABASE_URL / REDIS_URL and disables automation,
so no broker, database, or network call is made. The broker read fails without
credentials, which is exactly the "unavailable" state the dashboard must render
honestly instead of inventing a balance.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from trader.api.server import create_app


def _client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def test_summary_is_unavailable_not_fabricated():
    c = _client()
    r = c.get("/api/v2/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["equity"] is None
    assert body["day_pnl"] is None
    # The old implementation returned a hard-coded 10000.0 equity here.
    assert body["source"] == "unavailable"
    assert body["status"] == "unknown"


def test_positions_reports_unavailable_instead_of_empty_book(monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("broker offline")

    # Alpaca crypto data is readable without credentials, and a developer .env
    # may contain real paper keys, so force the unavailable path deterministically.
    monkeypatch.setattr("trader.api.server.Broker.account", boom)
    monkeypatch.setattr("trader.api.server.Broker.positions_detail", boom)
    c = _client()
    r = c.get("/api/v2/positions")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["positions"] == []
    assert body["total_unrealized_pl"] is None


def test_quotes_report_per_symbol_unavailable(monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("market data offline")

    monkeypatch.setattr("trader.api.server.MarketStore.many", boom)
    c = _client()
    r = c.get("/api/v2/quotes")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert len(body["quotes"]) <= 8
    for q in body["quotes"]:
        assert q["available"] is False
        assert q["error"]


def test_db_endpoint_without_database_is_explicit():
    c = _client()
    body = c.get("/api/v2/db").json()
    assert body["configured"] is False
    assert body["ok"] is True  # durable store is optional in this env
    assert body["detail"] == "no DATABASE_URL configured"


def test_config_exposes_safe_dashboard_fields_only():
    body = _client().get("/api/config/validate").json()
    assert body["trading_mode"] == "paper"
    assert body["live"] is False
    assert body["password_required"] is False
    assert "max_order_notional" in body["risk"]
    assert "take_profit_pct" in body
    # No secrets are ever exposed by the config endpoint.
    assert "alpaca_api_key" not in body
    assert "database_url" not in body


def test_auth_reports_whether_password_is_required():
    body = _client().post("/api/auth", json={"password": ""}).json()
    assert body == {"ok": True, "password_required": False}


def test_performance_is_empty_until_a_backtest_runs():
    body = _client().get("/api/v2/performance").json()
    assert body["source"] == "none"
    assert "message" in body


def test_security_headers_on_api_and_dashboard():
    c = _client()
    api = c.get("/api/health")
    assert api.headers["x-frame-options"] == "DENY"
    assert api.headers["x-content-type-options"] == "nosniff"
    assert api.headers["cache-control"] == "no-store"
    page = c.get("/")
    assert page.status_code == 200
    assert "content-security-policy" in page.headers
    assert "script-src 'self'" in page.headers["content-security-policy"]


def test_cancel_accepts_password_from_body_only(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    c = _client()
    # Query-string passwords would leak into proxy logs and browser history.
    assert c.post("/api/jobs/nope/cancel?password=s3cret", json={}).status_code == 403
    ok = c.post("/api/jobs/nope/cancel", json={"password": "s3cret"})
    assert ok.status_code == 200
    assert ok.json() == {"canceled": False}


def test_unknown_order_trips_kill_switch_even_if_persistence_fails(monkeypatch):
    """A durable-store failure must never delay the kill switch on ambiguity."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    c = _client()

    from trader.execution.broker import Fill
    from trader.api.server import Broker, DBStore

    monkeypatch.setattr(Broker, "account",
                        lambda self: {"equity": 10000.0, "bp": 10000.0, "day_pnl": 0.0})
    monkeypatch.setattr(Broker, "positions", lambda self: {})
    monkeypatch.setattr(Broker, "send", lambda self, *a, **k: Fill(
        "api-test", "unknown", "BTC/USD", "BUY", 100.0, 0.0, None,
        "submit failed", "2026-10-26T00:00:00Z"))

    def boom(self, *a, **k):
        raise RuntimeError("durable store write failed")

    monkeypatch.setattr(DBStore, "save_order", boom)
    r = c.post("/api/orders", json={"symbol": "BTC/USD", "side": "BUY",
                                    "notional": 100, "password": "pw"})
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is False
    assert body["reconcile_required"] is True
    assert body["persistence_error"]
    assert c.get("/api/risk").json()["killed"] is True


def test_filled_order_survives_persistence_failure(monkeypatch):
    """Persistence errors are reported without rewriting a confirmed fill."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    c = _client()

    from trader.execution.broker import Fill
    from trader.api.server import Broker, DBStore

    monkeypatch.setattr(Broker, "account",
                        lambda self: {"equity": 10000.0, "bp": 10000.0, "day_pnl": 0.0})
    monkeypatch.setattr(Broker, "positions", lambda self: {})
    monkeypatch.setattr(Broker, "send", lambda self, *a, **k: Fill(
        "api-test", "filled", "BTC/USD", "BUY", 100.0, 100.0, 100.0, "",
        "2026-10-26T00:00:00Z"))

    monkeypatch.setattr(DBStore, "save_order",
                        lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    r = c.post("/api/orders", json={"symbol": "BTC/USD", "side": "BUY",
                                    "notional": 100, "password": "pw"})
    assert r.status_code == 200
    body = r.json()
    assert body["approved"] is True
    assert body["order"]["status"] == "filled"
    assert body["persistence_error"]


def test_dashboard_html_has_no_inline_script_and_references_assets():
    html = _client().get("/").text
    assert "/static/app.js" in html
    assert "/static/config.js" in html
    assert "/static/styles.css" in html
    # CSP script-src 'self' would block inline scripts.
    assert "<script>" not in html
