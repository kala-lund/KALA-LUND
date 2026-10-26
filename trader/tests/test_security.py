from __future__ import annotations

import json
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient
from trader.api.server import create_app
from trader.core.kit import check_ops, public_error, public_text, safe_msg


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
    # Readiness (deployment gate) and overall health are distinct fields.
    assert body["ok"] in (True, False)
    assert body["ready"] in (True, False)
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["storage"], dict) and "durable" in body["storage"]
    for name in ("config", "risk_kill", "db", "redis", "broker"):
        assert name in body["components"], name
    assert "ts" in body


# ---------------------------------------------------------------------------
# Authenticated mutations (fail-closed), authenticated reads, API docs,
# CORS, rate limiting and dependency-aware health reporting.
# ---------------------------------------------------------------------------
GATED_READS = ("/api/v2/summary", "/api/v2/positions", "/api/v2/orders",
               "/api/v2/performance", "/api/v2/signals", "/api/signals",
               "/api/logs")
PUBLIC_READS = ("/api/health", "/api/v2/health", "/health", "/healthz",
                "/api/config/validate", "/api/v2/market", "/api/v2/db",
                "/api/v2/risk", "/api/v2/automate/status", "/api/v2/muse")

# (path, body) for every state-changing route, including expensive compute.
MUTATIONS = (
    ("/api/orders", {"symbol": "BTC/USD", "side": "BUY", "notional": 10}),
    ("/api/kill", {"reason": "test"}),
    ("/api/pipeline/run", {}),
    ("/api/backtest", {"symbol": "BTC/USD"}),
    ("/api/walkforward", {}),
    ("/api/automate/start", {}),
    ("/api/automate/stop", {}),
    ("/api/automate/run-once", {}),
    ("/api/jobs/nope/cancel", {}),
)


def _pw_client(monkeypatch, password: str = "pw-test") -> TestClient:
    monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    return TestClient(create_app(), raise_server_exceptions=False)


def _hermetic_heavy_work(monkeypatch) -> None:
    """Neutralize compute/broker work so auth checks stay hermetic."""

    class _Job:
        id = "job-test"
        status = "queued"

    monkeypatch.setattr("trader.api.server.run_cycle",
                        lambda *a, **k: {"signals": {}})
    monkeypatch.setattr("trader.api.server.Jobs.submit",
                        lambda self, kind, fn: _Job())


def test_mutations_require_auth(monkeypatch):
    """Anonymous and wrong-password mutations are rejected before any work."""
    _hermetic_heavy_work(monkeypatch)
    c = _pw_client(monkeypatch)
    for path, body in MUTATIONS:
        anon = c.post(path, json=body)
        assert anon.status_code == 403, f"{path} anonymous: {anon.status_code}"
        assert anon.json()["error"] is True
        wrong = c.post(path, json={**body, "password": "wrong"})
        assert wrong.status_code == 403, f"{path} wrong pw: {wrong.status_code}"


def test_mutations_fail_closed_without_configured_password():
    """No DASHBOARD_PASSWORD means locked, never 'open by default'."""
    c = TestClient(create_app(), raise_server_exceptions=False)
    r = c.post("/api/walkforward", json={})
    assert r.status_code == 403
    assert "not configured" in r.json()["detail"]


def test_authorized_mutations_are_not_rejected(monkeypatch):
    _hermetic_heavy_work(monkeypatch)
    c = _pw_client(monkeypatch)
    ok = {"password": "pw-test"}
    assert c.post("/api/pipeline/run", json=ok).status_code == 200
    assert c.post("/api/walkforward", json=ok).status_code == 200
    assert c.post("/api/kill", json={**ok, "reason": "test"}).status_code == 200
    assert c.post("/api/jobs/nope/cancel", json=ok).status_code == 200
    assert c.post("/api/positions", json=ok).status_code == 200
    start = c.post("/api/automate/start", json=ok)
    assert start.status_code == 200
    assert c.post("/api/automate/stop", json=ok).status_code == 200
    bt = c.post("/api/backtest", json={"symbol": "BTC/USD", **ok})
    assert bt.status_code == 200 and bt.json()["job_id"] == "job-test"


def test_mutations_accept_the_header_as_well_as_the_body(monkeypatch):
    """Body first, header as an alternative; both are out-of-band of the URL."""
    _hermetic_heavy_work(monkeypatch)
    c = _pw_client(monkeypatch)
    hdr = {"X-Dashboard-Password": "pw-test"}
    assert c.post("/api/pipeline/run", headers=hdr).status_code == 200
    assert c.post("/api/walkforward", headers=hdr).status_code == 200
    assert c.post("/api/jobs/nope/cancel").status_code == 403


def test_password_in_query_string_is_never_accepted(monkeypatch):
    """A credential in the URL would leak into proxy logs and history."""
    c = _pw_client(monkeypatch)
    assert c.post("/api/kill?password=pw-test", json={}).status_code == 403
    assert c.post("/api/jobs/nope/cancel?password=pw-test", json={}).status_code == 403
    assert c.get("/api/v2/summary?password=pw-test").status_code == 403


def test_account_reads_require_auth_when_password_configured(monkeypatch):
    c = _pw_client(monkeypatch)
    hdr = {"X-Dashboard-Password": "pw-test"}
    for path in GATED_READS:
        anon = c.get(path)
        assert anon.status_code == 403, f"{path} anonymous: {anon.status_code}"
        good = c.get(path, headers=hdr)
        assert good.status_code == 200, f"{path} with header: {good.status_code}"
    bad = c.get("/api/v2/summary", headers={"X-Dashboard-Password": "nope"})
    assert bad.status_code == 403


def test_health_market_and_diagnostics_stay_public(monkeypatch):
    c = _pw_client(monkeypatch)
    for path in PUBLIC_READS:
        r = c.get(path)
        assert r.status_code in (200, 503), f"{path}: {r.status_code}"


def test_read_credentials_cannot_be_bruteforced(monkeypatch):
    c = _pw_client(monkeypatch)
    codes = [c.get("/api/v2/summary",
                   headers={"X-Dashboard-Password": f"guess-{i}"}).status_code
             for i in range(16)]
    assert 403 in codes and 429 in codes, codes


def test_expensive_compute_is_rate_limited(monkeypatch):
    _hermetic_heavy_work(monkeypatch)
    c = _pw_client(monkeypatch)
    codes = [c.post("/api/pipeline/run", json={"password": "pw-test"}).status_code
             for _ in range(9)]
    assert codes.count(200) <= 6, codes
    assert 429 in codes, codes


def test_interactive_docs_are_disabled_by_default():
    c = TestClient(create_app(), raise_server_exceptions=False)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404, path


def test_interactive_docs_require_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ENABLE_API_DOCS", "true")
    c = TestClient(create_app(), raise_server_exceptions=False)
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/docs").status_code == 200


def test_cors_wildcard_is_rejected(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        create_app()


def test_cors_untrusted_origin_gets_no_allow_header(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://dash.example.com")
    c = TestClient(create_app(), raise_server_exceptions=False)
    trusted = c.get("/api/health", headers={"Origin": "https://dash.example.com"})
    assert trusted.headers.get("access-control-allow-origin") == "https://dash.example.com"
    untrusted = c.get("/api/health", headers={"Origin": "https://evil.example.net"})
    assert "access-control-allow-origin" not in untrusted.headers


def test_health_is_ok_when_broker_and_store_are_available(monkeypatch):
    from trader.api.server import Broker

    monkeypatch.setattr(Broker, "probe", lambda self: {
        "paper": True, "endpoint": "https://paper-api.alpaca.markets",
        "available": True, "status": "ok", "detail": "Paper account reachable.",
        "latency_ms": 8.0, "account_status": "ACTIVE"})
    c = TestClient(create_app(), raise_server_exceptions=False)
    body = c.get("/api/health").json()
    assert body["ok"] is True and body["ready"] is True and body["status"] == "ok"
    assert body["components"]["broker"]["ok"] is True
    assert body["components"]["broker"]["account_status"] == "ACTIVE"


def test_broker_outage_degrades_health_without_failing_readiness(monkeypatch):
    """A rejected/absent broker must be visible but must not 503 the service."""
    from trader.api.server import Broker

    monkeypatch.setattr(Broker, "probe", lambda self: {
        "paper": True, "endpoint": "https://paper-api.alpaca.markets",
        "available": False, "status": "auth_failed",
        "detail": "Alpaca rejected the paper API credentials (HTTP 401/403).",
        "latency_ms": 42.0, "account_status": None})
    c = TestClient(create_app(), raise_server_exceptions=False)
    for path in ("/api/health", "/api/v2/health", "/health", "/healthz"):
        r = c.get(path)
        assert r.status_code == 200, path
        body = r.json()
        assert body["ready"] is True
        assert body["ok"] is False and body["status"] == "degraded"
        assert body["components"]["broker"]["ok"] is False
        assert body["components"]["broker"]["status"] == "auth_failed"


def test_redis_outage_degrades_health_without_failing_readiness(monkeypatch):
    from trader.data.redis_cache import RedisCache

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(RedisCache, "_init_redis", lambda self: None)
    monkeypatch.setattr(RedisCache, "health_check", lambda self: {
        "status": "error", "latency_ms": None, "error": "redis not available",
        "backend": "memory", "configured": True, "fallback": True})
    c = TestClient(create_app(), raise_server_exceptions=False)
    r = c.get("/api/v2/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["ok"] is False and body["status"] == "degraded"
    assert body["components"]["redis"]["ok"] is False
    assert body["components"]["redis"]["fallback"] is True
    assert body["components"]["redis"]["backend"] == "memory"


def test_hung_broker_probe_does_not_block_the_health_endpoint(monkeypatch):
    """A hung Alpaca call must not make health checks time out (restart loop)."""
    from trader.api.server import Broker

    def slow_probe(self):
        time.sleep(8.0)
        return {"paper": True, "available": True, "status": "ok",
                "detail": "late", "latency_ms": 8000.0, "account_status": "ACTIVE"}

    monkeypatch.setattr(Broker, "probe", slow_probe)
    c = TestClient(create_app(), raise_server_exceptions=False)
    start = time.monotonic()
    r = c.get("/api/v2/health")
    elapsed = time.monotonic() - start
    assert r.status_code == 200
    assert elapsed < 4.5, f"health blocked for {elapsed:.1f}s"
    assert r.json()["components"]["broker"]["status"] == "probing"


def test_public_error_strips_hosts_and_ip_literals():
    msg = public_error(RuntimeError("connect failed to 10.1.2.3:5432"))
    assert "10.1.2.3" not in msg and "[redacted-ip]" in msg
    msg2 = public_text("could not connect: host=db.internal.example:5432 refused")
    assert "db.internal.example" not in msg2
    assert "host=[redacted]" in msg2


def test_db_diagnostics_do_not_leak_topology(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/finrl")

    from trader.api.server import DBStore

    monkeypatch.setattr(DBStore, "_init_db", lambda self: None)

    def boom(self):
        raise RuntimeError("could not connect to 10.1.2.3:5432")

    monkeypatch.setattr(DBStore, "verify", boom)
    monkeypatch.setattr(DBStore, "stats", lambda self: {
        "backend": "postgres", "configured": True, "connected": False,
        "durable": False, "degraded": True, "fallback_memory": False,
        "error": "host=db.internal.example down"})
    body = TestClient(create_app(), raise_server_exceptions=False).get("/api/v2/db").json()
    blob = json.dumps(body)
    assert "10.1.2.3" not in blob
    assert "db.internal.example" not in blob
    assert body["ok"] is False and body["durable"] is False
