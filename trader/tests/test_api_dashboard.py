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
    # A confirmed but unrecorded fill is untracked risk: halt new submissions.
    assert c.get("/api/risk").json()["killed"] is True


def test_dashboard_html_has_no_inline_script_and_references_assets():
    html = _client().get("/").text
    assert "/static/app.js" in html
    assert "/static/config.js" in html
    assert "/static/styles.css" in html
    # CSP script-src 'self' would block inline scripts.
    assert "<script>" not in html


# ---------------------------------------------------------------------------
# Durable-store requirements: health truthfulness, diagnostics coherence and
# the pre-submission order gate. The DBStore is stubbed (no PostgreSQL in CI).
# ---------------------------------------------------------------------------
def _durable_client(monkeypatch, ok: bool, password: str = "") -> TestClient:
    monkeypatch.setenv("REQUIRE_DURABLE_STORE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/finrl")
    if password:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)

    from trader.api.server import DBStore

    monkeypatch.setattr(DBStore, "_init_db", lambda self: None)
    monkeypatch.setattr(DBStore, "durable_available", lambda self: {
        "ok": ok, "backend": "postgres",
        "detail": "postgres" if ok else "connection refused"})
    monkeypatch.setattr(DBStore, "health_check", lambda self: {
        "status": "ok" if ok else "error",
        "latency_ms": 1.0 if ok else None,
        "error": None if ok else "connection refused"})
    monkeypatch.setattr(DBStore, "stats", lambda self: {
        "backend": "postgres", "configured": True, "connected": ok,
        "durable": ok, "degraded": not ok, "fallback_memory": False,
        "error": None if ok else "connection refused",
        "signals": 0, "backtests": 0, "orders": 0})
    monkeypatch.setattr(DBStore, "verify", lambda self: {
        "ok": ok, "schema_version": 1 if ok else None, "expected_version": 1,
        "tables": {"signals": ok}, "missing_tables": [], "missing_columns": {},
        "missing_indexes": [], "counts": {"signals": 0},
        "error": None if ok else "connection refused"})
    return TestClient(create_app(), raise_server_exceptions=False)


def test_health_is_503_when_required_durable_store_is_down(monkeypatch):
    c = _durable_client(monkeypatch, ok=False)
    for path in ("/api/v2/health", "/health", "/healthz"):
        r = c.get(path)
        assert r.status_code == 503, path
        body = r.json()
        assert body["ready"] is False
        assert body["status"] == "degraded"
        assert body["components"]["db"]["ok"] is False
        assert body["components"]["db"]["durable"] is False


def test_health_is_200_when_required_durable_store_is_available(monkeypatch):
    c = _durable_client(monkeypatch, ok=True)
    for path in ("/api/v2/health", "/health"):
        r = c.get(path)
        assert r.status_code == 200, path
        body = r.json()
        assert body["ready"] is True
        assert body["status"] == "ok"
        assert body["components"]["db"]["backend"] == "postgres"
        assert body["components"]["db"]["durable"] is True


def test_db_diagnostics_never_contradict_the_health_probe(monkeypatch):
    """Regression: 'ok: true' next to 'backend=memory, durable=false'."""
    c = _durable_client(monkeypatch, ok=False)
    body = c.get("/api/v2/db").json()
    assert body["backend"] == "postgres"
    assert body["degraded"] is True
    assert body["durable"] is False
    assert body["ok"] is False
    assert body["ready"] is False
    assert body["require_durable"] is True

    healthy = _durable_client(monkeypatch, ok=True)
    good = healthy.get("/api/v2/db").json()
    assert good["ok"] is True
    assert good["durable"] is True
    assert good["connected"] is True
    assert good["degraded"] is False


def test_order_refused_before_submission_when_durable_store_is_down(monkeypatch):
    c = _durable_client(monkeypatch, ok=False, password="pw")

    from trader.api.server import Broker

    def must_not_send(self, *a, **k):
        raise AssertionError("broker must not be called without durable storage")

    monkeypatch.setattr(Broker, "send", must_not_send)
    r = c.post("/api/orders", json={"symbol": "BTC/USD", "side": "BUY",
                                    "notional": 50, "password": "pw"})
    assert r.status_code == 503
    body = r.json()
    assert body["approved"] is False
    assert body["durable_store"]["ok"] is False
    assert "refused before submission" in body["reasons"][0]


def test_summary_reports_broker_timeout_without_500(monkeypatch):
    """A broker read timeout is an unavailable account, not a server error."""
    def boom(self, *a, **k):
        raise TimeoutError("Broker read 'account' exceeded 10.0s")

    monkeypatch.setattr("trader.api.server.Broker.account", boom)
    monkeypatch.setattr("trader.api.server.Broker.positions_detail", boom)
    r = _client().get("/api/v2/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["equity"] is None
    assert "exceeded" in body["error"]


def test_db_diagnostics_probe_before_taking_the_status_snapshot(monkeypatch):
    """verify() reconnects, so it must run before stats() is captured."""
    _durable_client(monkeypatch, ok=True)

    from trader.api.server import DBStore, create_app

    calls: list[str] = []
    monkeypatch.setattr(DBStore, "verify", lambda self: (
        calls.append("verify") or {
            "ok": True, "schema_version": 1, "expected_version": 1,
            "tables": {"signals": True}, "missing_tables": [], "missing_columns": {},
            "missing_indexes": [], "counts": {"signals": 0}, "error": None}))
    monkeypatch.setattr(DBStore, "stats", lambda self: (
        calls.append("stats") or {
            "backend": "postgres", "configured": True, "connected": True,
            "durable": True, "degraded": False, "fallback_memory": False,
            "error": None, "signals": 0, "backtests": 0, "orders": 0}))

    body = TestClient(create_app(), raise_server_exceptions=False).get("/api/v2/db").json()
    assert calls == ["verify", "stats"]
    assert body["ok"] is True and body["durable"] is True


def test_kill_switch_reports_ok_false_but_stays_http_200(monkeypatch):
    """An engaged kill switch must not look like an infrastructure outage.

    Restarting the process on this signal would clear the in-memory kill switch.
    """
    c = _durable_client(monkeypatch, ok=True, password="pw")
    assert c.post("/api/kill", json={"password": "pw", "reason": "test"}).status_code == 200

    for path in ("/api/v2/health", "/health"):
        r = c.get(path)
        assert r.status_code == 200, path
        body = r.json()
        assert body["ok"] is False
        assert body["ready"] is True
        assert body["status"] == "degraded"
        assert body["components"]["risk_kill"]["ok"] is False
