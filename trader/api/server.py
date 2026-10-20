from __future__ import annotations

"""Trader API: health, config, market, pipeline, backtest jobs, guarded orders, risk, automate."""

import os
import time
import uuid
from typing import Any

import atexit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..ai import Muse
from ..backtest.bt import run as bt_run, windows
from ..core.config import load_settings, validate_settings
from ..core.kit import Auth, check_ops, cid, init_session_logging, log, qty as v_qty, recent, safe_msg, set_cid, shutdown_session_logging, symbol as v_sym
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
    app = FastAPI(title="Trader", version="2.0.0")
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
        return await call_next(request)

    prov = CcxtProvider() if s.data_provider == "ccxt" else AlpacaProvider(s.alpaca_api_key, s.alpaca_api_secret)
    cache = RedisCache(s.cache_max, s.cache_ttl, s.redis_url)
    store = MarketStore(prov, cache, s.fetch_workers)
    db = DBStore(s.database_url, require_durable=s.require_durable_store)
    risk = Risk(Limits(s.max_order_notional, s.max_position_notional, s.max_exposure,
                       s.max_concentration, s.max_daily_loss, s.max_drawdown,
                       s.max_open_orders, s.min_buying_power, s.stale_seconds))
    jobs = Jobs(2)
    ai = Muse(s.claude_enabled, s.claude_api_key, s.claude_model, s.claude_base_url,
              s.claude_timeout, s.claude_max_tokens)
    broker = Broker(s.alpaca_api_key, s.alpaca_api_secret, s.alpaca_paper_base_url,
                    s.enable_paper_orders, broker_environment=s.trading_mode)
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
        return True

    def authed(pw: str, key: str = "default") -> None:
        if not _rl(key):
            raise HTTPException(429, "Rate limit exceeded. Try again later.")
        a: Auth = check_ops(pw, s.dashboard_password)
        if not a.ok:
            raise HTTPException(403, a.reason)

    @app.get("/api/health")
    def health():
        errs = validate_settings(s)
        cache_stats, db_stats = _cached_health("health_stats", lambda: (cache.stats(), db.stats()))
        return {"ok": not errs and not risk.killed,
                "components": {
                    "config": {"ok": not errs, "detail": "; ".join(errs) or "ok"},
                    "risk_kill": {"ok": not risk.killed, "detail": "engaged" if risk.killed else "ok"},
                    "Muse": {"ok": True, "detail": "enabled" if ai.available else "disabled/fail-closed"},
                    "rl": {"ok": True, "detail": _SB3_VERSION},
                    "redis": {"ok": True, "detail": f"{cache_stats.get('backend','memory')}"},
                    "db": {"ok": True, "detail": f"{db_stats.get('backend','memory')}"},
                },
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    @app.get("/api/config/validate")
    def cfg():
        errs = validate_settings(s)
        return {"valid": not errs, "errors": errs, "trading_mode": s.trading_mode,
                "paper_orders": s.enable_paper_orders, "live": s.enable_live_trading,
                "symbols": list(s.crypto_symbols), "n_symbols": len(s.crypto_symbols),
                "timeframe": s.primary_timeframe,
                "automate": {"enabled": s.automate_enabled, "minutes": s.automate_minutes,
                             "dry_run": s.automate_dry_run, "min_conf": s.automate_min_conf}}

    @app.get("/api/logs")
    def logs(limit: int = 200, level: str = ""):
        lv = tuple(level.split(",")) if level else ()
        return {"events": recent(min(limit, 1000), lv)}
    @app.get("/api/market/bars")
    def bars(symbol: str, timeframe: str = "15Min", limit: int = 120):
        try:
            b = store.bars(v_sym(symbol), timeframe, min(limit, 500))
            return {"symbol": canon(symbol), "timeframe": timeframe, "bars": b[-120:],
                    "quality": qcheck(b, TF_MS.get(timeframe, 900_000), s.stale_seconds)}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, safe_msg(exc))

    @app.post("/api/pipeline/run")
    def pipe():
        return cycle()

    @app.get("/api/signals")
    def sigs():
        return state["signals"]

    @app.post("/api/backtest")
    def bt(req: BTReq):
        def _job():
            b = store.bars(v_sym(req.symbol), req.timeframe, min(req.limit, 2000))
            f = features(b)
            w = []
            for i in range(len(b)):
                sub = f[:i + 1]
                g = Trend().run(req.symbol, sub) if len(sub) >= 51 else None
                w.append(g.signal * 0.5 if g else 0.0)
            r = bt_run(b, w, 10000.0, s.fee_pct, s.slippage_pct)
            # Persist backtest to DB
            db.save_backtest(req.symbol, req.timeframe, r.metrics, r.assumptions)
            return {"metrics": r.metrics, "curve": r.curve, "trades": r.trades[:200],
                    "assumptions": r.assumptions, "symbol": req.symbol}
        j = jobs.submit("backtest", _job)
        return {"job_id": j.id, "status": j.status}

    @app.post("/api/walkforward")
    def wf(n: int = 1000, tr: int = 400, va: int = 200, te: int = 200, step: int = 200):
        return {"windows": [vars(w) for w in windows(n, tr, va, te, step)]}

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        j = jobs.jobs.get(jid)
        if not j:
            raise HTTPException(404, "unknown job")
        return vars(j)

    @app.post("/api/jobs/{jid}/cancel")
    def job_cancel(jid: str, password: str = ""):
        authed(password, "cancel")
        return {"canceled": jobs.cancel(jid)}

    @app.get("/api/risk")
    def risk_v():
        return {"killed": risk.killed, "limits": vars(risk.lim)}

    @app.post("/api/kill")
    def kill(req: dict):
        authed(req.get("password", ""), "kill")
        risk.trip(req.get("reason", "manual"))
        log("kill", level="warning", reason=req.get("reason", "manual"))
        return {"killed": True}

    @app.post("/api/orders")
    def orders(req: OrderReq):
        authed(req.password, "orders")
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
            # Persist order to DB
            db.save_order(sym, req.side, notional, f.status, f.cid or "")
            if f.status == "unknown":
                risk.trip("Order outcome unknown; reconciliation required.")
                return {"approved": False, "order": vars(f), "reconcile_required": True}
            if f.status not in ("filled", "partially_filled", "submitted", "accepted",
                                "new", "pending_new", "held"):
                return {"approved": False, "order": vars(f), "execution_rejected": True}
            return {"approved": True, "order": vars(f)}
        except PermissionError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, safe_msg(exc))

    @app.post("/api/positions")
    def pos(req: dict = {}):
        # positions is read-only account state; no auth required.
        # Served through the short-TTL display cache to avoid repeated Alpaca calls.
        def _fetch_positions():
            try:
                a = broker.account()
                pos_list = broker.positions_detail()
                total_up = sum(float(p.get("unrealized_pl", 0.0) or 0.0) for p in pos_list)
                total_cost = sum(float(p.get("cost_basis", 0.0) or 0.0) for p in pos_list)
                total_up_pct = (total_up / total_cost * 100.0) if total_cost else 0.0
                return {"account": a, "positions": pos_list,
                        "total_unrealized_pl": round(total_up, 2),
                        "total_unrealized_pl_pct": round(total_up_pct, 2)}
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(502, safe_msg(exc))
        return _cached_display("positions", _fetch_positions)

    @app.post("/api/auth")
    def auth(req: dict = {}):
        """Session password gate. Returns 200 if password correct, 403 otherwise.
        If no DASHBOARD_PASSWORD is configured, access is granted (local dev).
        Rate-limited to prevent password brute-force."""
        if s.dashboard_password:
            if not _rl("auth", limit=10, window=60.0):
                raise HTTPException(429, "Rate limit exceeded. Try again later.")
            a: Auth = check_ops(req.get("password", ""), s.dashboard_password)
            if not a.ok:
                raise HTTPException(403, a.reason)
        return {"ok": True}

    @app.get("/api/automate/status")
    def a_status():
        return vars(auto.st)

    @app.post("/api/automate/start")
    def a_start(req: dict):
        authed(req.get("password", ""), "automate")
        return vars(auto.start())

    @app.post("/api/automate/stop")
    def a_stop(req: dict):
        authed(req.get("password", ""), "automate")
        return vars(auto.stop())

    @app.post("/api/automate/run-once")
    def a_once(req: dict):
        authed(req.get("password", ""), "automate")
        return auto.once()

    # ===== V2 API (versioned, minimal) ============
    @app.get("/api/v2/health")
    def api_health():
        cs, ds = _cached_health("v2_health_stats", lambda: (cache.stats(), db.stats()))
        errs = validate_settings(s)
        return {"ok": not errs and not risk.killed, "mode": s.trading_mode,
                "components": {
                    "config": {"ok": not errs, "detail": "; ".join(errs) or "ok"},
                    "risk_kill": {"ok": not risk.killed, "detail": "engaged" if risk.killed else "ok"},
                    "db": {"ok": True, "detail": ds.get("backend", "unknown")},
                    "redis": {"ok": True, "detail": cs.get("backend", "unknown")},
                }}

    @app.get("/api/v2/market")
    def api_market():
        return {"symbols": list(s.crypto_symbols),
                "timeframe": s.primary_timeframe,
                "source": s.data_provider}

    @app.get("/api/v2/positions")
    def api_positions():
        def _fetch_v2_positions():
            try:
                a = broker.account()
                pos_list = broker.positions_detail()
                return {"account": a, "positions": pos_list,
                        "total_unrealized_pl": round(sum(float(p.get("unrealized_pl", 0.0) or 0.0) for p in pos_list), 2)}
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(502, safe_msg(exc))
        return _cached_display("v2_positions", _fetch_v2_positions)

    @app.get("/api/v2/signals")
    def api_signals_v2():
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
            try:
                a = broker.account()
            except Exception:  # noqa: BLE001 - credentials may be absent; fail gracefully
                a = {"equity": state["equity"], "bp": state["equity"], "status": "unknown", "pdt": False}
            eq = float(a.get("equity", state["equity"]) or 0.0)
            peak = max(state.get("peak", eq), eq)
            dd = ((peak - eq) / peak * 100.0) if peak > 0 else 0.0
            try:
                pos_list = broker.positions_detail()
            except Exception:  # noqa: BLE001
                pos_list = []
            up = sum(float(p.get("unrealized_pl", 0.0) or 0.0) for p in pos_list)
            return {"equity": round(eq, 2), "day_pnl": round(state.get("day_pnl", 0.0), 2),
                    "peak": round(peak, 2), "drawdown_pct": round(dd, 2),
                    "unrealized_pl": round(up, 2), "buying_power": float(a.get("bp", 0.0) or 0.0),
                    "status": a.get("status", "unknown"), "positions": len(pos_list),
                    "take_profit_pct": s.take_profit_pct}
        return _cached_display("equity_snapshot", _fetch)

    @app.get("/api/v2/summary")
    def api_summary_v2():
        return _equity_snapshot()

    @app.get("/api/v2/orders")
    def api_orders_v2():
        def _fetch():
            open_orders, oerr = broker.get_open_orders()
            hist = [vars(f) for f in broker.history][-100:]
            try:
                db_open, derr = db.get_open_orders()
            except Exception:  # noqa: BLE001
                db_open, derr = [], "db open-orders read unavailable"
            return {"open": open_orders or [], "open_error": oerr,
                    "history": hist, "db_open": db_open, "db_error": derr}
        return _cached_display("v2_orders", _fetch)

    @app.get("/api/v2/muse")
    def api_muse_v2():
        return {"enabled": bool(s.claude_enabled), "available": ai.available,
                "model": s.claude_model, "last_error": ai.err, "failures": ai._fails,
                "cooling_down": time.time() < ai._cool, "fail_closed": True}

    @app.get("/api/v2/performance")
    def api_performance_v2():
        best = None
        for j in reversed(list(jobs.jobs.values())):
            if j.kind == "backtest" and j.status == "done" and j.result:
                best = j.result; break
        if best:
            return {"source": "latest_backtest_job", "symbol": best.get("symbol"),
                    "metrics": best.get("metrics"), "assumptions": best.get("assumptions"),
                    "curve": best.get("curve")}
        return {"source": "none",
                "message": "No completed backtest. Run one from the Backtest tab."}

    import pathlib
    fe = pathlib.Path(FE_DIR).resolve()
    if fe.exists():
        app.mount("/static", StaticFiles(directory=str(fe)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(fe / "index.html"))

    return app


app = create_app()
