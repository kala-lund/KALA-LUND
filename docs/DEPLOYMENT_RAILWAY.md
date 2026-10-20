# Deployment: Railway (Python Backend)

**Target:** Railway
**Service:** FinRL Trader V2 FastAPI backend
**Entry point (verified):** `python scripts/run_trader.py`
**Health check (verified):** `GET /api/v2/health` → HTTP 200

---

## 1. Architecture

```
scripts/run_trader.py
  └─ trader/core/config.py    load_settings() + validate_settings()   (fail-fast)
  └─ uvicorn.run("trader.api.server:app", host=s.api_host, port=s.api_port)
       └─ trader/api/server.py  FastAPI app + static dashboard mount
```

- `trader/core/config.py` resolves the listen port as
  `API_PORT` → `WEB_PORT` → `PORT` → `8000`, so Railway's dynamic `$PORT`
  is honoured automatically.
- `railway.toml` sets `API_HOST=0.0.0.0`; without it the service would bind to
  `127.0.0.1` and Railway could not route traffic to it.
- The build uses `Dockerfile.trader` (Python 3.11 slim). Railway does not
  auto-detect non-standard Dockerfile names, so `[build].dockerfilePath`
  is required.

## 2. Required service variables (dashboard, names only — never values)

| Variable | Required | Notes |
|---|---|---|
| `TRADING_MODE` | yes | must be `paper` (`railway.toml` default) |
| `ENABLE_LIVE_TRADING` | yes | must be `false` (`railway.toml` default) |
| `ENABLE_PAPER_ORDERS` | no | `false` (default) disables order submission entirely |
| `DATABASE_URL` | **yes** | PostgreSQL/Supabase DSN; durable order/signal/backtest state |
| `REQUIRE_DURABLE_STORE` | **yes** | `true` (default in `railway.toml`) — the service refuses to start if `DATABASE_URL` is missing or unreachable, instead of silently using ephemeral in-memory storage |
| `CORS_ORIGINS` | **yes** | comma-separated frontend origins, e.g. `https://finrl-trader.pages.dev` — **not** set in `railway.toml` because the real Pages domain is only known after the frontend is created |
| `DASHBOARD_PASSWORD` | strongly recommended | gates `/api/orders`, `/api/kill`, `/api/automate/*` |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | for market data + paper orders | Alpaca **paper** credentials only |
| `ALPACA_PAPER_BASE_URL` | no | defaults to `https://paper-api.alpaca.markets`; any other value is rejected at startup |
| `REDIS_URL` | no | optional; falls back to an in-process cache |
| `CLAUDE_ENABLED` / `CLAUDE_API_KEY` | no | optional Muse AI decision support (fail-closed, never blocks the trading loop) |
| `CRYPTO_SYMBOLS` / `PRIMARY_TIMEFRAME` | no | e.g. `BTC/USD,ETH/USD,SOL/USD` / `15Min` |

`PORT` is supplied by Railway — do not set it manually.

## 3. Deploy

1. Railway dashboard → **New Project → Deploy from GitHub repo** → select this repo.
2. Railway detects `railway.toml` and uses `Dockerfile.trader`.
3. Add the service variables listed above.
4. Deploy. Check the logs for the line
   `Trader V2 on http://0.0.0.0:<port> (paper, live disabled)`.
5. Verify the health check: `https://<your-service>.up.railway.app/api/v2/health`.

Local equivalent:

```bash
railway init
railway up
```

## 4. Health check

`GET /api/v2/health` returns HTTP 200 with a JSON body:

```json
{"ok": true, "mode": "paper",
 "components": {"config": {"ok": true, "detail": "ok"},
                "risk_kill": {"ok": true, "detail": "ok"},
                "db": {"ok": true, "detail": "postgresql"},
                "redis": {"ok": true, "detail": "memory"}}}
```

- `ok=false` means either the configuration is invalid or the kill switch is
  engaged; read `components.config.detail` / `components.risk_kill.detail`.
- The endpoint performs no broker call, so it stays fast and does not require
  Alpaca credentials.
- `GET /api/health` is the richer variant (adds `Muse` and `rl` components).

## 5. Paper-only safeguards (must stay enabled)

- `validate_settings()` rejects `ENABLE_LIVE_TRADING=true` and any
  `TRADING_MODE` other than `paper`; `scripts/run_trader.py` then exits `1`
  before the server starts.
- The broker rejects any base URL that is not the approved Alpaca paper endpoint
  (`trader/execution/safety.py`).
- The dashboard password gate protects order, kill-switch and automation routes.
- `REQUIRE_DURABLE_STORE=true` prevents the service from running with ephemeral
  storage.

**Never** set `ENABLE_LIVE_TRADING=true`, and never point
`ALPACA_PAPER_BASE_URL` at `api.alpaca.markets`.

## 6. Manual dashboard actions (cannot be automated)

1. Create the service and add the variables above.
2. Create the PostgreSQL/Supabase database and copy its DSN into `DATABASE_URL`.
3. Set `CORS_ORIGINS` to the exact frontend origin (no wildcards).
4. Generate a Railway domain, or attach a custom domain (CNAME to
   `*.up.railway.app`); Railway provisions TLS automatically.
5. Confirm the deploy log line and the `/api/v2/health` response.

## 7. Notes / limitations

- The risk **kill switch is process-local** (`trader/risk/guard.py`). A restart
  clears it; orders are still re-validated against the risk limits on every
  submission, and the broker-side paper account remains the source of truth.
- Session error logs are written to the container filesystem (`errors/`) and are
  therefore ephemeral on Railway — they are diagnostics, not trading state.
- Railway free-tier services may sleep; the dashboard shows a connection warning
  until the backend wakes up.
- Redis and Supabase are optional for local development; in production the
  database is mandatory because of `REQUIRE_DURABLE_STORE`.
