from __future__ import annotations

"""Trader API: health, config, market, pipeline, backtest jobs, guarded orders, risk, automate."""

import os
import threading
import time
import uuid
from typing import Any

import atexit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..ai import Muse
from ..backtest.bt import run as bt_run, windows
from ..core.config import load_settings, validate_settings
from ..core.kit import Auth, check_ops, cid, init_session_logging, log, public_error, public_text, qty as v_qty, recent, safe_msg, set_cid, shutdown_session_logging, symbol as v_sym
from ..data.market import AlpacaProvider, Cache, MarketStore, TF_MS, canon, CcxtProvider
from ..data.redis_cache import RedisCache
from ..data.db_store import DBStore
from ..data.quality import check as qcheck
from ..engine.loop import Loop, run_cycle
from ..execution.broker import Broker
from ..features.feat import features
from ..monitoring import Jobs
from ..risk.guard import Account, Limits, Risk
from ..strategies.strats import Trend

FE_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BTReq(BaseModel):
    symbol: str = "BTC/USD"
    timeframe: str = "15Min"
    limit: int = 300
    password: str = ""


class OrderReq(BaseModel):
    symbol: str = "BTC/USD"
    side: str = "BUY"
    notional: float = 100.0
    password: str = ""


def _err(status: int, code: str, detail: object):
    return JSONResponse(status_code=status, content={
        "error": True, "code": code, "status": status, "detail": detail})


def create_app():
    s = load_settings()
    config_errors = validate_settings(s)
    if config_errors:
        raise RuntimeError("Invalid trading configuration: " + "; ".join(config_errors))
    # Interactive schema docs are opt-in: a public deployment must not advertise
    # its endpoints. ENABLE_API_DOCS=true restores /docs, /redoc, /openapi.json.
    _docs = bool(s.api_docs_enabled)
    app = FastAPI(
        title="Trader", version="2.0.0",
        docs_url="/docs" if _docs else None,
        redoc_url="/redoc" if _docs else None,
        openapi_url="/openapi.json" if _docs else None,
    )
    session_id = init_session_logging(BASE_DIR)
    atexit.register(shutdown_session_logging)
    app.add_middleware(CORSMiddleware, allow_origins=list(s.cors_origins) + [
        f"http://127.0.0.1:{s.api_port}",
        f"http://localhost:{s.api_port}",
    ], allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        log("http_error", level="warning", path=request.url.path, status=exc.status_code)
        return _err(exc.status_code, "http_error", exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _val(request: Request, exc: RequestValidationError):
        log("validation_error", level="warning", path=request.url.path)
        return _err(422, "validation_error", exc.errors())

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        import traceback
        # redact the traceback before logging; return only a safe message to the client
        tb = "".join(traceback.format_exception_only(type(exc), exc))[-300:]
        log("unhandled", level="error", path=request.url.path, error=tb,
            module="trader.api.server", operation="http", exc=safe_msg(exc),
            message="unhandled request error")
        return _err(500, "internal_error", safe_msg(exc))

    @app.middleware("http")
    async def _cid(request: Request, call_next):
        set_cid(request.headers.get("X-Request-ID", uuid.uuid4().hex[:8]))
        response = await call_next(request)
        # Baseline hardening for a dashboard that must never be framed or sniffed.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.path == "/" and response.headers.get(
                "content-type", "").startswith("text/html"):
            # The dashboard's only inline script was removed, so scripts must be
            # same-origin files. API calls are same-origin when FastAPI serves it.
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'")
        return response

    prov = CcxtProvider() if s.data_provider == "ccxt" else AlpacaProvider(s.alpaca_api_key, s.alpaca_api_secret)
    cache = RedisCache(s.cache_max, s.cache_ttl, s.redis_url)
    store = MarketStore(prov, cache, s.fetch_workers)
    db = DBStore(
        s.database_url,
        connect_timeout=s.database_connect_timeout,
        require_durable=s.require_durable_store,
        connect_retries=s.database_connect_retries,
        sslmode=s.database_sslmode,
        application_name=s.database_application_name,
    )
    risk = Risk(Limits(s.max_order_notional, s.max_position_notional, s.max_exposure,
                       s.max_concentration, s.max_daily_loss, s.max_drawdown,
                       s.max_open_orders, s.min_buying_power, s.stale_seconds))
    jobs = Jobs(2)
    ai = Muse(s.claude_enabled, s.claude_api_key, s.claude_model, s.claude_base_url,
              s.claude_timeout, s.claude_max_tokens)
    broker = Broker(s.alpaca_api_key, s.alpaca_api_secret, s.alpaca_paper_base_url,
                    s.enable_paper_orders, broker_environment=s.trading_mode,
                    read_timeout=s.broker_read_timeout)
    # Release database / cache / broker worker resources on process shutdown.
    atexit.register(db.close)
    atexit.register(cache.close)
    atexit.register(broker.close)
    state: dict = {"signals": {}, "equity": 10000.0, "day_pnl": 0.0, "peak": 10000.0}

    # Short-TTL read-only display cache for broker account/positions data.
    # Used ONLY by dashboard summary/orders endpoints for display.
    # NEVER used for risk validation or order authorization (risk engine uses its
    # own AccountState constructed separately). TTL = 5 seconds.
    _DISPLAY_CACHE: dict[str, tuple[float, Any]] = {}
    _DISPLAY_TTL_SECONDS = 5.0

    # Cache the stable-baselines3 version at module load so /api/health does not
    # re-import a heavy package on every request. Falls back gracefully if sb3 is absent.
    _SB3_VERSION: str | None = None
    try:
        import stable_baselines3 as _sb3_mod
        _SB3_VERSION = f"sb3 {_sb3_mod.__version__}"
    except Exception:  # noqa: BLE001 - sb3 is optional for health reporting
        _SB3_VERSION = "unavailable"

    def _cached_display(key: str, fetch) -> Any:
        now = time.time()
        hit = _DISPLAY_CACHE.get(key)
        if hit is not None and now - hit[0] < _DISPLAY_TTL_SECONDS:
            return hit[1]
        val = fetch()
        _DISPLAY_CACHE[key] = (now, val)
        return val

    # Cache health-check stats (Redis INFO memory + DB counts) so they are not
    # re-fetched on every health-check request. Read-only display data only;
    # never used for risk validation or order authorization. TTL = 5 seconds.
    _HEALTH_CACHE: dict[str, tuple[float, dict]] = {}
    _HEALTH_TTL_SECONDS = 5.0

    def _cached_health(key: str, fetch) -> dict:
        now = time.time()
        hit = _HEALTH_CACHE.get(key)
        if hit is not None and now - hit[0] < _HEALTH_TTL_SECONDS:
            return hit[1]
        val = fetch()
        _HEALTH_CACHE[key] = (now, val)
        return val

    def cycle():
        out = run_cycle(list(s.crypto_symbols), s.primary_timeframe, dict(s.weights),
                        store, s.stale_seconds, s.max_concentration, state["equity"], ai=ai)
        state["signals"] = out.get("signals", {})
        # Persist signals to DB
        for sym, sig in out.get("signals", {}).items():
            agg = sig.get("aggregate") or {}
            db.save_signal(sym, agg)
        return out

    def live_acct():
        try:
            a = broker.account()
            eq = float(a.get("equity", 0.0) or 0.0)
            day_pnl = a.get("day_pnl")
            if day_pnl is None:
                return None
            peak = max(state.get("peak", eq), eq)
            state["peak"] = peak
            return Account(eq, float(a.get("bp", 0.0) or 0.0), broker.positions(),
                           0, float(day_pnl), peak, 0.0)
        except Exception:  # noqa: BLE001
            return None

    auto = Loop(s, cycle, risk, broker, live_acct, db=db)
    if s.automate_enabled:
        try:
            auto.start()
        except Exception as exc:  # noqa: BLE001 - reconcile/Alpaca failure must not block API startup
            log("auto_start_failed", level="error", error=safe_msg(exc))

    # rate limiting for password-checked endpoints: (ip, endpoint) -> [timestamps]
    _rate: dict[str, list[float]] = {}

    def _rl(key: str, limit: int = 10, window: float = 60.0) -> bool:
        now = time.time()
        hits = _rate.get(key, [])
        hits = [t for t in hits if now - t < window]
        if len(hits) >= limit:
            return False
        hits.append(now)
        _rate[key] = hits
        # Bound memory if a client rotates keys; only expired buckets are pruned.
        if len(_rate) > 2048:
            for k in [k for k, v in _rate.items() if not v or now - v[-1] > window]:
                _rate.pop(k, None)
        return True

    def _client_ip(request: Request) -> str:
        # X-Forwarded-For is client-controlled unless a trusted proxy appends the
        # real address. Honouring it by default would let anyone rotate the header
        # and bypass the password rate limit. Railway: set TRUST_PROXY_HEADERS=true.
        if s.trust_proxy_headers:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                # The right-most entry is the one appended by the edge proxy.
                return forwarded.split(",")[-1].strip()[:64] or "unknown"
        return (request.client.host if request.client else "unknown")[:64]

    def authed(pw: str, key: str = "default", request: Request | None = None,
               limit: int = 10, window: float = 60.0) -> None:
        # Rate limit per (client, endpoint) so one client cannot lock out others.
        # Expensive compute endpoints pass a tighter limit than a plain mutation.
        client = _client_ip(request) if request is not None else "local"
        if not _rl(f"{key}:{client}", limit, window):
            raise HTTPException(429, "Rate limit exceeded. Try again later.")
        # Body first (existing contract); the header is accepted for clients that
        # cannot easily send a body. A URL query string is never consulted.
        supplied = pw or (request.headers.get("x-dashboard-password", "")
                          if request is not None else "")
        a: Auth = check_ops((supplied or "").strip(), s.dashboard_password)
        if not a.ok:
            raise HTTPException(403, a.reason)

    def _body_pw(req: object) -> str:
        """Password from a JSON body (never from a URL query string)."""
        if isinstance(req, dict):
            v = req.get("password", "")
            return v if isinstance(v, str) else ""
        return ""

    # Distinct throttle bucket for failed read credentials: a GET must not
    # become an unlimited password oracle. Successful reads are cheap and are
    # not throttled (the dashboard polls several panels per refresh).
    def read_guard(request: Request, pw: str = "") -> None:
        """Authorize account-data reads when a dashboard password is configured.

        Market data, health and configuration diagnostics stay public. Portfolio,
        order, log, signal and performance data are account information, so they
        require the dashboard credential once one exists. Local/dev installs with
        no DASHBOARD_PASSWORD stay open (there is no credential to fail closed
        against) and keep the existing dashboard experience.

        The password is accepted in the ``X-Dashboard-Password`` header (preferred,
        since GET has no body) or in a POST body field -- never a query string.
        """
        if not s.dashboard_password:
            return
        client = _client_ip(request)
        supplied = (request.headers.get("x-dashboard-password") or pw or "").strip()
        a: Auth = check_ops(supplied, s.dashboard_password)
        if a.ok:
            return
        if not _rl(f"readfail:{client}", limit=12, window=60.0):
            raise HTTPException(429, "Too many invalid credential attempts.")
        raise HTTPException(403, a.reason)

    def _db_component() -> dict:
        """Report the durable store truthfully.

        A configured store is actively probed (cached for 5 s) because ``stats()``
        is query-free: a silently dropped idle connection would otherwise keep
        /health green until the next write failed. When REQUIRE_DURABLE_STORE is
        set, an unavailable database is a hard failure (ok=false) rather than a
        silent in-memory fallback.
        """
        stats = db.stats()
        if not stats.get("configured"):
            if s.require_durable_store:
                return {"ok": False, "detail": "durable store required but no DATABASE_URL",
                        "configured": False, "backend": "none", "durable": False}
            return {"ok": True, "detail": "memory (no DATABASE_URL configured)",
                    "configured": False, "backend": "memory", "durable": False}
        store = _cached_health("db_probe", db.durable_available)
        if store.get("ok"):
            return {"ok": True, "detail": "postgres", "backend": "postgres",
                    "durable": True, "latency_ms": store.get("latency_ms")}
        return {"ok": not s.require_durable_store,
                "detail": "postgres unavailable",
                "backend": "postgres", "durable": False,
                "latency_ms": store.get("latency_ms")}

    def _redis_component() -> dict:
        """Report the cache layer truthfully.

        A configured Redis is probed with a real PING (bounded by the client's
        2 s socket timeout, cached for 5 s) instead of being inferred from INFO
        memory stats: availability must reflect connectivity. When Redis is down
        the process keeps serving from its in-process cache -- that is a degraded
        cache, not a readiness failure, so it never causes a restart loop.
        """
        hc = _cached_health("redis_ping", cache.health_check)
        configured = bool(hc.get("configured", bool(s.redis_url)))
        if hc.get("status") == "ok":
            return {"ok": True, "configured": True, "detail": "redis",
                    "backend": "redis", "latency_ms": hc.get("latency_ms"),
                    "fallback": False}
        if configured:
            return {"ok": False, "configured": True, "backend": "memory",
                    "detail": "redis unavailable; in-process cache active",
                    "latency_ms": None, "fallback": True,
                    "error": hc.get("error")}
        return {"ok": True, "configured": False, "backend": "memory",
                "detail": "memory (no REDIS_URL)", "latency_ms": None,
                "fallback": False}

    # Broker probe state: an Alpaca round-trip must never block a health check.
    # The probe runs on a daemon thread; the first caller waits at most
    # _PROBE_WAIT seconds for a real answer and later callers reuse a result up
    # to _PROBE_TTL seconds old. A hung Alpaca endpoint therefore cannot trip an
    # orchestrator health-check timeout and cause a restart loop.
    _PROBE_TTL = 15.0
    _PROBE_WAIT = 3.0
    _probe_lock = threading.Lock()
    _probe_state: dict = {"result": None, "at": 0.0, "inflight": False}

    def _run_probe() -> None:
        try:
            out = broker.probe()
        except Exception as exc:  # noqa: BLE001 - health must never crash
            out = {"paper": True, "endpoint": getattr(broker, "url", ""),
                   "available": False, "status": "broker_error",
                   "detail": public_error(exc), "latency_ms": None,
                   "account_status": None}
        with _probe_lock:
            _probe_state["result"] = out
            _probe_state["at"] = time.time()
            _probe_state["inflight"] = False

    def _broker_component() -> dict:
        now = time.time()
        with _probe_lock:
            result = _probe_state["result"]
            fresh = result is not None and now - _probe_state["at"] < _PROBE_TTL
            inflight = bool(_probe_state["inflight"])
            if not fresh and not inflight:
                _probe_state["inflight"] = True
                inflight = True
                threading.Thread(target=_run_probe, name="broker-probe",
                                 daemon=True).start()
        if result is None and inflight:
            # Bounded first wait: a healthy probe answers in milliseconds, while
            # a hung endpoint falls through to "probing" instead of blocking.
            deadline = time.time() + _PROBE_WAIT
            while time.time() < deadline:
                with _probe_lock:
                    if _probe_state["result"] is not None:
                        break
                time.sleep(0.02)
        with _probe_lock:
            result = _probe_state["result"]
            at = _probe_state["at"]
        if result is None:
            return {"ok": False, "available": False, "status": "probing",
                    "detail": "broker probe in progress", "paper": True,
                    "endpoint": getattr(broker, "url", "")}
        out = dict(result)
        out["ok"] = bool(out.get("available"))
        out["stale_seconds"] = round(max(0.0, time.time() - at), 1)
        return out

    def _health_payload(include_extras: bool = True) -> dict:
        errs = validate_settings(s)
        dbf, rf, bf = _db_component(), _redis_component(), _broker_component()
        comps: dict[str, dict] = {
            "config": {"ok": not errs, "detail": "; ".join(errs) or "ok"},
            "risk_kill": {"ok": not risk.killed, "detail": "engaged" if risk.killed else "ok"},
            "db": dbf,
            "redis": rf,
            "broker": bf,
        }
        if include_extras:
            comps["Muse"] = {"ok": True, "detail": "enabled" if ai.available else "disabled/fail-closed"}
            comps["rl"] = {"ok": True, "detail": _SB3_VERSION}
        # "ready" is the deployment gate (can this instance safely take traffic?):
        # valid config plus a working durable store when one is required. Broker
        # and Redis outages are deliberately excluded: they degrade functionality
        # but must not restart a process (a restart would also clear the kill
        # switch) and are recoverable without one.
        storage_ready = dbf["ok"] if s.require_durable_store else True
        ready = (not errs) and storage_ready
        # "ok" is overall health: a missing/rejected broker credential, a dead
        # broker endpoint or a Redis outage must not silently appear healthy.
        ok = (not errs) and (not risk.killed) and dbf["ok"] and rf["ok"] and bf["ok"]
        return {"ok": ok,
                "ready": ready, "status": "ok" if ok else "degraded",
                "mode": s.trading_mode,
                "storage": {"durable": bool(dbf.get("durable")),
                            "backend": dbf.get("backend"),
                            "required": s.require_durable_store},
                "components": comps,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    @app.get("/api/health")
    def health():
        return _health_payload(include_extras=True)

    @app.get("/api/config/validate")
    def cfg():
        errs = validate_settings(s)
        return {"valid": not errs, "errors": errs, "trading_mode": s.trading_mode,
                "paper_orders": s.enable_paper_orders, "live": s.enable_live_trading,
                "symbols": list(s.crypto_symbols), "n_symbols": len(s.crypto_symbols),
                "timeframe": s.primary_timeframe, "data_provider": s.data_provider,
                "take_profit_pct": s.take_profit_pct,
                "fee_pct": s.fee_pct, "slippage_pct": s.slippage_pct,
                "password_required": bool(s.dashboard_password),
                "durable_store_required": s.require_durable_store,
                "risk": {"max_order_notional": s.max_order_notional,
                         "max_position_notional": s.max_position_notional,
                         "max_exposure": s.max_exposure,
                         "max_concentration": s.max_concentration,
                         "max_daily_loss": s.max_daily_loss,
                         "max_drawdown": s.max_drawdown,
                         "max_open_orders": s.max_open_orders,
                         "stale_seconds": s.stale_seconds},
                "automate": {"enabled": s.automate_enabled, "minutes": s.automate_minutes,
                             "dry_run": s.automate_dry_run, "min_conf": s.automate_min_conf}}

    @app.get("/api/logs")
    def logs(request: Request, limit: int = 200, level: str = ""):
        read_guard(request)
        lv = tuple(level.split(",")) if level else ()
        return {"events": recent(min(limit, 1000), lv)}
    @app.get("/api/market/bars")
    def bars(symbol: str, timeframe: str = "15Min", limit: int = 120):
        try:
            tf = timeframe if timeframe in TF_MS else "15Min"
            n = max(1, min(int(limit), 500))
            b = store.bars(v_sym(symbol), tf, n)
            return {"symbol": canon(symbol), "timeframe": tf, "bars": b[-120:],
                    "quality": qcheck(b, TF_MS.get(tf, 900_000), s.stale_seconds)}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, safe_msg(exc))

    @app.post("/api/pipeline/run")
    def pipe(request: Request, req: dict = {}):
        # Runs the full signal/risk cycle: a state-changing, broker-touching
        # operation, so it requires the dashboard credential and is rate limited.
        authed(_body_pw(req), "pipeline", request, limit=6)
        return cycle()

    @app.get("/api/signals")
    def sigs(request: Request):
        read_guard(request)
        return state["signals"]

    @app.post("/api/backtest")
    def bt(request: Request, req: BTReq):
        # Expensive compute that also persists a row: authenticated + throttled.
        authed(req.password, "backtest", request, limit=6)
        def _job():
            b = store.bars(v_sym(req.symbol), req.timeframe, min(max(1, req.limit), 2000))
            f = features(b)
            w = []
            for i in range(len(b)):
                sub = f[:i + 1]
                g = Trend().run(req.symbol, sub) if len(sub) >= 51 else None
                w.append(g.signal * 0.5 if g else 0.0)
            r = bt_run(b, w, 10000.0, s.fee_pct, s.slippage_pct)
            # Benchmark: buy-and-hold the same instrument over the same bars with
            # no fees. Valid prices only, so it aligns with bt.run's curve.
            pxs: list[float] = []
            ts: list[int] = []
            for bar in b:
                try:
                    px = float(bar["c"])
                except (TypeError, ValueError, KeyError):
                    continue
                if px <= 0:
                    continue
                pxs.append(px)
                ts.append(int(bar.get("ts", 0) or 0))
            bench = [round(10000.0 * p / pxs[0], 2) for p in pxs] if pxs else []
            bench_ret = (bench[-1] / 10000.0 - 1.0) if bench else 0.0
            # Persist backtest to DB
            db.save_backtest(req.symbol, req.timeframe, r.metrics, r.assumptions)
            return {"metrics": r.metrics, "curve": r.curve, "trades": r.trades[:200],
                    "assumptions": r.assumptions, "symbol": req.symbol,
                    "benchmark": bench, "curve_ts": ts[:len(r.curve)],
                    "benchmark_return": round(bench_ret, 6)}
        j = jobs.submit("backtest", _job)
        return {"job_id": j.id, "status": j.status}

    @app.post("/api/walkforward")
    def wf(request: Request, req: dict = {}, n: int = 1000, tr: int = 400, va: int = 200,
           te: int = 200, step: int = 200):
        authed(_body_pw(req), "walkforward", request, limit=30)
        return {"windows": [vars(w) for w in windows(n, tr, va, te, step)]}

    @app.get("/api/jobs/{jid}")
    def job(jid: str, request: Request):
        read_guard(request)
        j = jobs.jobs.get(jid)
        if not j:
            raise HTTPException(404, "unknown job")
        return vars(j)

    @app.post("/api/jobs/{jid}/cancel")
    def job_cancel(jid: str, request: Request, req: dict[str, Any] | None = Body(default=None)):
        """Cancel a queued/running job.

        The password travels in the JSON body, never in the URL/query string,
        so it cannot leak into proxy access logs or browser history.
        """
        authed((req or {}).get("password", ""), "cancel", request)
        return {"canceled": jobs.cancel(jid)}

    @app.get("/api/risk")
    def risk_v():
        return {"killed": risk.killed, "limits": vars(risk.lim)}

    @app.post("/api/kill")
    def kill(request: Request, req: dict):
        authed(req.get("password", ""), "kill", request)
        risk.trip(req.get("reason", "manual"))
        log("kill", level="warning", reason=req.get("reason", "manual"))
        return {"killed": True}

    @app.post("/api/orders")
    def orders(request: Request, req: OrderReq):
        authed(req.password, "orders", request)
        if s.require_durable_store:
            # Fail closed BEFORE the broker is contacted. Submitting first and
            # discovering the order cannot be recorded leaves a live paper order
            # with no durable trace of it.
            store = db.durable_available()
            if not store.get("ok"):
                log("order_refused_storage", level="error", symbol=req.symbol,
                    error=store.get("detail"))
                return JSONResponse(status_code=503, content={
                    "approved": False,
                    "reasons": ["Durable store unavailable; order refused before submission."],
                    "durable_store": {"ok": False, "detail": store.get("detail")}})
        try:
            sym, notional = v_sym(req.symbol), v_qty(req.notional)
            acct = live_acct()
            cid = f"api-{uuid.uuid4().hex[:8]}"
            d = risk.check(sym, req.side, notional, acct, cid=cid)
            if not d.ok:
                log("order_rejected", reasons=d.reasons, symbol=sym)
                return {"approved": False, "reasons": d.reasons}
            f = broker.send(sym, req.side, notional=notional, cid=cid)
            log("order", symbol=sym, side=req.side, status=f.status)

            def _persist() -> str | None:
                """Persist the order record without ever changing the execution verdict.

                With REQUIRE_DURABLE_STORE a failed write raises. That must not turn
                a confirmed submission into an HTTP 400 nor delay the kill switch on
                an ambiguous outcome, so the caller reports it separately.
                """
                try:
                    db.save_order(sym, req.side, notional, f.status, f.cid or "")
                    return None
                except Exception as exc:  # noqa: BLE001 - report, don't mask the order
                    msg = safe_msg(exc)
                    # The order was already submitted: without a durable record it
                    # is untracked risk, so halt further submissions (same rule as
                    # the automation loop).
                    risk.trip("Order could not be recorded; reconciliation required.")
                    log("order_persist_failed", level="error", symbol=sym,
                        cid=f.cid, status=f.status, error=msg)
                    return msg

            if f.status == "unknown":
                # Fail closed before any I/O: the order may exist at the broker.
                risk.trip("Order outcome unknown; reconciliation required.")
                persist_err = _persist()
                out = {"approved": False, "order": vars(f), "reconcile_required": True}
                if persist_err:
                    out["persistence_error"] = persist_err
                return out
            persist_err = _persist()
            if f.status not in ("filled", "partially_filled", "submitted", "accepted",
                                "new", "pending_new", "held"):
                out = {"approved": False, "order": vars(f), "execution_rejected": True}
                if persist_err:
                    out["persistence_error"] = persist_err
                return out
            out = {"approved": True, "order": vars(f)}
            if persist_err:
                out["persistence_error"] = persist_err
            return out
        except PermissionError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, safe_msg(exc))

    @app.post("/api/positions")
    def pos(request: Request, req: dict = {}):
        # Account state is read-only here (no mutation), but it is account data:
        # once a dashboard password exists it requires the credential. The
        # legacy POST accepts the password in the body; GET-style callers use
        # the X-Dashboard-Password header. Local installs without a password
        # stay open so the dashboard keeps working unchanged.
        read_guard(request, _body_pw(req))
        # Served through the short-TTL display cache to avoid repeated Alpaca
        # calls, and degrades honestly when the broker is unreachable (200 +
        # available=false + error) instead of a 502 that the dashboard would
        # silently render as a flat book.
        def _fetch_positions():
            try:
                a = broker.account()
                pos_list = broker.positions_detail()
                total_up = sum(float(p.get("unrealized_pl", 0.0) or 0.0) for p in pos_list)
                total_cost = sum(float(p.get("cost_basis", 0.0) or 0.0) for p in pos_list)
                total_up_pct = (total_up / total_cost * 100.0) if total_cost else 0.0
                return {"account": a, "positions": pos_list, "available": True, "error": None,
                        "total_unrealized_pl": round(total_up, 2),
                        "total_unrealized_pl_pct": round(total_up_pct, 2)}
            except Exception as exc:  # noqa: BLE001 - broker read failure must not 502 the widget
                return {"account": None, "positions": [], "available": False,
                        "error": safe_msg(exc),
                        "total_unrealized_pl": 0.0, "total_unrealized_pl_pct": 0.0}
        return _cached_display("positions", _fetch_positions)

    @app.post("/api/auth")
    def auth(request: Request, req: dict = {}):
        """Session password gate. Returns 200 if password correct, 403 otherwise.
        If no DASHBOARD_PASSWORD is configured, access is granted (local dev).
        Rate-limited per client to prevent password brute-force."""
        if s.dashboard_password:
            if not _rl(f"auth:{_client_ip(request)}"):
                raise HTTPException(429, "Rate limit exceeded. Try again later.")
            a: Auth = check_ops(req.get("password", ""), s.dashboard_password)
            if not a.ok:
                raise HTTPException(403, a.reason)
        return {"ok": True, "password_required": bool(s.dashboard_password)}

    @app.get("/api/automate/status")
    def a_status():
        return vars(auto.st)

    @app.post("/api/automate/start")
    def a_start(request: Request, req: dict):
        authed(req.get("password", ""), "automate", request)
        return vars(auto.start())

    @app.post("/api/automate/stop")
    def a_stop(request: Request, req: dict):
        authed(req.get("password", ""), "automate", request)
        return vars(auto.stop())

    @app.post("/api/automate/run-once")
    def a_once(request: Request, req: dict):
        authed(req.get("password", ""), "automate", request)
        return auto.once()

    # ===== V2 API (versioned, minimal) ============
    @app.get("/api/v2/health")
    def api_health():
        # Readiness for the platform router (Railway healthcheck). Returns 503
        # when this instance cannot safely take traffic: invalid config, or
        # REQUIRE_DURABLE_STORE with an unreachable durable store.
        body = _health_payload(include_extras=True)
        return JSONResponse(status_code=200 if body["ready"] else 503, content=body)

    @app.get("/health")
    @app.get("/healthz")
    def health_readiness():
        # Readiness for external monitors/uptime checks: HTTP 503 when this
        # instance cannot safely serve traffic (durable store required but
        # unavailable, or invalid config), 200 otherwise. Same body as
        # /api/v2/health. An engaged kill switch is reported as ok=false but
        # stays HTTP 200 on purpose: a restarting orchestrator would clear it.
        body = _health_payload(include_extras=True)
        return JSONResponse(status_code=200 if body["ready"] else 503, content=body)

    @app.get("/api/v2/market")
    def api_market():
        return {"symbols": list(s.crypto_symbols),
                "timeframe": s.primary_timeframe,
                "source": s.data_provider}

    @app.get("/api/v2/positions")
    def api_positions(request: Request):
        read_guard(request)
        def _fetch_v2_positions():
            try:
                a = broker.account()
                pos_list = broker.positions_detail()
                return {"available": True, "error": None, "account": a,
                        "positions": pos_list,
                        "total_unrealized_pl": round(sum(float(p.get("unrealized_pl", 0.0) or 0.0) for p in pos_list), 2),
                        "positions_value": round(sum(float(p.get("market_value", 0.0) or 0.0) for p in pos_list), 2)}
            except Exception as exc:  # noqa: BLE001 - report, never fake a flat book
                return {"available": False, "error": public_error(exc),
                        "account": None, "positions": [],
                        "total_unrealized_pl": None, "positions_value": None}
        return _cached_display("v2_positions", _fetch_v2_positions)

    @app.get("/api/v2/signals")
    def api_signals_v2(request: Request):
        read_guard(request)
        return {"signals": state.get("signals", {}),
                "count": len(state.get("signals", {}))}

    def acct_daily_loss() -> float:
        """Read-only display value: session day P&L from in-memory state.

        Never used for risk validation or order authorization.
        """
        return float(state.get("day_pnl", 0.0) or 0.0)

    @app.get("/api/v2/risk")
    def api_risk_v2():
        return {"killed": risk.killed,
                "limits": vars(risk.lim),
                "daily_loss": acct_daily_loss()}

    @app.get("/api/v2/automate/status")
    def api_automate_status():
        return vars(auto.st)

    # ===== V2 dashboard summary / orders / muse / performance ============
    def _equity_snapshot():
        def _fetch():
            err = None
            try:
                a = broker.account()
            except Exception as exc:  # noqa: BLE001 - credentials may be absent
                a, err = None, public_error(exc)
            try:
                pos_list = broker.positions_detail()
            except Exception as exc:  # noqa: BLE001
                pos_list = []
                err = err or public_error(exc)
            if a is None:
                # Never invent equity/P&L: report the data as unavailable.
                return {"available": False, "source": "unavailable",
                        "error": err or "broker account unavailable",
                        "equity": None, "cash": None, "buying_power": None,
                        "day_pnl": None, "unrealized_pl": None,
                        "peak": None, "drawdown_pct": None,
                        "positions": 0, "positions_value": None,
                        "exposure_pct": None, "status": "unknown",
                        "take_profit_pct": s.take_profit_pct,
                        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            eq = float(a.get("equity", 0.0) or 0.0)
            peak = max(state.get("peak", eq), eq)
            dd = ((peak - eq) / peak * 100.0) if peak > 0 else 0.0
            up = sum(float(p.get("unrealized_pl", 0.0) or 0.0) for p in pos_list)
            pos_value = sum(float(p.get("market_value", 0.0) or 0.0) for p in pos_list)
            return {"available": True, "source": "broker", "error": err,
                    "equity": round(eq, 2),
                    "cash": round(float(a.get("cash", 0.0) or 0.0), 2),
                    "day_pnl": round(float(a.get("day_pnl", 0.0) or 0.0), 2),
                    "peak": round(peak, 2), "drawdown_pct": round(dd, 2),
                    "unrealized_pl": round(up, 2),
                    "buying_power": round(float(a.get("bp", 0.0) or 0.0), 2),
                    "positions_value": round(pos_value, 2),
                    "exposure_pct": round((pos_value / eq * 100.0) if eq > 0 else 0.0, 2),
                    "status": a.get("status", "unknown"), "positions": len(pos_list),
                    "take_profit_pct": s.take_profit_pct,
                    "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return _cached_display("equity_snapshot", _fetch)

    @app.get("/api/v2/summary")
    def api_summary_v2(request: Request):
        read_guard(request)
        return _equity_snapshot()

    @app.get("/api/v2/orders")
    def api_orders_v2(request: Request):
        read_guard(request)
        def _fetch():
            open_orders, oerr = broker.get_open_orders()
            hist = [vars(f) for f in list(broker.history)][-100:]
            try:
                db_open, derr = db.get_open_orders()
            except Exception:  # noqa: BLE001
                db_open, derr = [], "db open-orders read unavailable"
            return {"open": open_orders or [], "open_error": public_text(oerr) or None,
                    "history": hist, "db_open": db_open,
                    "db_error": public_text(derr) or None,
                    "broker_available": oerr is None,
                    "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return _cached_display("v2_orders", _fetch)

    @app.get("/api/v2/quotes")
    def api_quotes_v2():
        """Watchlist quote snapshot for the configured universe.

        Uses the cached market store; per-symbol failures are reported as
        ``available=false`` rather than being silently dropped or zero-filled.
        """
        def _fetch():
            symbols = list(s.crypto_symbols)[:8]
            tf = s.primary_timeframe
            try:
                raw = store.many(symbols, tf, s.fetch_bars)
            except Exception as exc:  # noqa: BLE001 - batch failure, report once
                return {"available": False, "error": safe_msg(exc),
                        "timeframe": tf, "quotes": [],
                        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            quotes = []
            for sym in symbols:
                data = raw.get(sym)
                if isinstance(data, Exception):
                    quotes.append({"symbol": sym, "available": False,
                                   "error": safe_msg(data)})
                    continue
                if not data:
                    quotes.append({"symbol": sym, "available": False,
                                   "error": "no bars returned"})
                    continue
                last = data[-1]
                window = data[-min(96, len(data)):]
                first = window[0]
                try:
                    last_px = float(last["c"])
                    first_px = float(first["c"])
                except (TypeError, ValueError, KeyError):
                    quotes.append({"symbol": sym, "available": False,
                                   "error": "malformed bar"})
                    continue
                change = last_px - first_px
                quotes.append({
                    "symbol": sym, "available": True, "error": None,
                    "last": round(last_px, 6),
                    "change": round(change, 6),
                    "change_pct": round((change / first_px * 100.0) if first_px else 0.0, 4),
                    "ts": int(last.get("ts", 0) or 0),
                    "bars": len(data),
                    "window_bars": len(window),
                    "quality": qcheck(data, TF_MS.get(tf, 900_000), s.stale_seconds),
                })
            return {"available": any(q["available"] for q in quotes),
                    "error": None, "timeframe": tf, "quotes": quotes,
                    "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return _cached_display("quotes", _fetch)

    @app.get("/api/v2/db")
    def api_db_v2():
        """Sanitized durable-store verification (schema, indexes, row counts)."""
        def _fetch():
            report: dict = {"configured": bool(s.database_url),
                            "require_durable": s.require_durable_store}
            if not s.database_url:
                report.update({"ok": not s.require_durable_store, "backend": "memory",
                               "connected": False, "durable": False, "degraded": False,
                               "fallback_memory": True, "error": None,
                               "detail": "no DATABASE_URL configured"})
                return report
            try:
                # Probe/verify BEFORE taking the status snapshot: verify()
                # reconnects once, so schema results and backend/durable fields
                # then describe the same moment instead of contradicting.
                report.update(db.verify())
            except Exception as exc:  # noqa: BLE001 - never leak a DSN
                report["ok"] = False
                report["error"] = public_error(exc)
            stats = db.stats()
            report.update({
                "backend": stats.get("backend"),
                "connected": bool(stats.get("connected")),
                "durable": bool(stats.get("durable")),
                "degraded": bool(stats.get("degraded")),
                "fallback_memory": bool(stats.get("fallback_memory")),
            })
            if not report.get("error"):
                # stats() carries a driver message; scrub host/IP detail before
                # it is returned to an unauthenticated diagnostics caller.
                report["error"] = public_text(stats.get("error") or "") or None
            # A single coherent verdict: the schema is valid AND writes are
            # actually persisted durably right now.
            report["ok"] = bool(report.get("ok")) and bool(report["durable"])
            report["ready"] = report["ok"]
            return report
        return _cached_health("db_verify", _fetch)

    @app.get("/api/v2/muse")
    def api_muse_v2():
        return {"enabled": bool(s.claude_enabled), "available": ai.available,
                "model": s.claude_model, "last_error": ai.err, "failures": ai._fails,
                "cooling_down": time.time() < ai._cool, "fail_closed": True}

    @app.get("/api/v2/performance")
    def api_performance_v2(request: Request):
        read_guard(request)
        best = None
        for j in reversed(list(jobs.jobs.values())):
            if j.kind == "backtest" and j.status == "done" and j.result:
                best = j.result
                break
        if best:
            return {"source": "latest_backtest_job", "symbol": best.get("symbol"),
                    "metrics": best.get("metrics"), "assumptions": best.get("assumptions"),
                    "curve": best.get("curve"), "benchmark": best.get("benchmark"),
                    "curve_ts": best.get("curve_ts"),
                    "benchmark_return": best.get("benchmark_return"),
                    "trades": best.get("trades", [])}
        return {"source": "none",
                "message": "No completed backtest. Run one from the Backtest tab."}

    import pathlib
    fe = pathlib.Path(FE_DIR).resolve()
    if fe.exists():
        app.mount("/static", StaticFiles(directory=str(fe)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(fe / "index.html"))

    # Warm the bounded broker probe at startup so the first health check has a
    # real answer instead of reporting "probing": a cold TLS handshake to Alpaca
    # was measured at ~4 s, while warm reads take ~0.3 s.
    with _probe_lock:
        _probe_state["inflight"] = True
    threading.Thread(target=_run_probe, name="broker-probe-warm",
                     daemon=True).start()

    return app


app = create_app()
