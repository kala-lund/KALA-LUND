from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from trader.api.server import create_app
from trader.core.kit import check_ops, safe_msg


def test_positions_read_only_no_auth():
    c = TestClient(create_app(), raise_server_exceptions=False)
    r = c.post("/api/positions", json={})
    assert r.status_code == 200
    body = r.json()
    assert "account" in body
    assert "positions" in body
    assert "total_unrealized_pl" in body
    assert "total_unrealized_pl_pct" in body


def test_rate_limiting_orders_endpoint():
    c = TestClient(create_app(), raise_server_exceptions=False)
    # many rapid requests without valid password → 429 eventually
    codes = set()
    for _ in range(25):
        rr = c.post("/api/orders", json={"symbol": "BTC/USD", "side": "BUY",
                                         "notional": 10, "password": "wrong"})
        codes.add(rr.status_code)
    assert 429 in codes, f"rate limit not triggered; codes={codes}"


def test_unauthorized_mutating_endpoints_403():
    c = TestClient(create_app(), raise_server_exceptions=False)
    for path, method in [("/api/kill", "post"), ("/api/automate/start", "post"),
                         ("/api/automate/stop", "post"), ("/api/automate/run-once", "post"),
                         ("/api/jobs/nope/cancel", "post")]:
        fn = getattr(c, method)
        rr = fn(path, json={"password": "wrong"})
        assert rr.status_code == 403, f"{method.upper()} {path}: {rr.status_code}"


def test_safe_msg_redacts_secrets():
    assert "redacted" in safe_msg(ValueError("my api key sk-abc123 leaked"))
    assert "redacted" in safe_msg(ValueError("token=secret123"))
    assert "redacted" not in safe_msg(ValueError("plain error message"))


def test_safe_msg_redacts_dsn_without_keyword_hint():
    msg = safe_msg(ValueError("invalid dsn: postgresql://user:hunter2@db.example.com:5432/x"))
    assert "hunter2" not in msg
    assert "user:" not in msg
    assert "[redacted]" in msg


def test_xff_spoofing_does_not_bypass_rate_limit(monkeypatch):
    """X-Forwarded-For is ignored unless TRUST_PROXY_HEADERS is enabled."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    c = TestClient(create_app(), raise_server_exceptions=False)
    codes = set()
    for i in range(25):
        rr = c.post("/api/auth", json={"password": "wrong"},
                    headers={"X-Forwarded-For": f"203.0.113.{i}"})
        codes.add(rr.status_code)
    assert 429 in codes, f"spoofed XFF bypassed the limit; codes={codes}"


def test_check_ops_rejects_empty_and_wrong():
    assert not check_ops("", "pw").ok
    assert not check_ops("wrong", "pw").ok
    assert check_ops("pw", "pw").ok
    assert not check_ops("pw", "").ok  # no password configured → locked


def test_health_shape():
    c = TestClient(create_app(), raise_server_exceptions=False)
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "components" in body
    assert "ts" in body
