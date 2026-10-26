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
- `Dockerfile.trader` sets `ENV API_HOST=0.0.0.0`; without it the service would
  bind to `127.0.0.1` and Railway could not route traffic to it. Railway cannot
  set environment variables from `railway.toml`, so every hard safety default
  (`TRADING_MODE`, `ENABLE_LIVE_TRADING`, `ENABLE_PAPER_ORDERS`,
  `REQUIRE_DURABLE_STORE`) is baked into the image instead.
- The build uses `Dockerfile.trader` (Python 3.11 slim). Railway does not
  auto-detect non-standard Dockerfile names, so `[build].builder = "DOCKERFILE"`
  and `[build].dockerfilePath` are both required. Without the explicit builder
  Railway defaults to Railpack for new services.

## 2. Required service variables (dashboard, names only — never values)

| Variable | Required | Notes |
|---|---|---|
| `TRADING_MODE` | no (do **not** override) | image ENV default `paper`; any other value is rejected at startup |
| `ENABLE_LIVE_TRADING` | no (do **not** override) | image ENV default `false`; live trading is prohibited |
| `ENABLE_PAPER_ORDERS` | no | image ENV default `false` disables order submission entirely |
| `DATABASE_URL` | **yes** | Neon **pooled** DSN (`...-pooler...` host, `?sslmode=require`); durable order/signal/backtest state. See `docs/DEPLOYMENT_NEON.md`. Must be correct **before** the first deploy (see 8.2) |
| `REQUIRE_DURABLE_STORE` | no (do **not** override with `false`) | `true`, baked into `Dockerfile.trader` — the service refuses to start if `DATABASE_URL` is missing or unreachable, instead of silently using ephemeral in-memory storage |
| `CORS_ORIGINS` | **yes** | comma-separated frontend origins, e.g. `https://finrl-trader.pages.dev` — **not** set in the image because the real Pages domain is only known after the frontend is created |
| `TRUST_PROXY_HEADERS` | recommended on Railway | `true` behind the Railway edge so password rate limiting uses the appended `X-Forwarded-For` address. Default `false` prevents header spoofing but puts all clients in one bucket |
| `DASHBOARD_PASSWORD` | strongly recommended | gates `/api/orders`, `/api/kill`, `/api/automate/*` |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | for market data + paper orders | Alpaca **paper** credentials only |
| `ALPACA_PAPER_BASE_URL` | no | defaults to `https://paper-api.alpaca.markets`; any other value is rejected at startup |
| `REDIS_URL` | no | optional; falls back to an in-process cache |
| `BROKER_READ_TIMEOUT_SECONDS` | no | default `10`; hard upper bound for **read-only** broker calls. alpaca-py never passes a timeout to `requests.Session`, so without this a hung connection can block an API request thread indefinitely. Order submission is deliberately **not** bounded this way. |
| `CLAUDE_ENABLED` / `CLAUDE_API_KEY` | no | optional Muse AI decision support (fail-closed, never blocks the trading loop) |
| `CRYPTO_SYMBOLS` / `PRIMARY_TIMEFRAME` | no | e.g. `BTC/USD,ETH/USD,SOL/USD` / `15Min` |
| `DB_CONNECT_TIMEOUT` / `DB_CONNECT_RETRIES` | no | default `5` s / `3`; bounded Neon connect attempts |
| `DB_SSLMODE` | no | default `require` (auto-applied to non-local hosts) |
| `DB_APPLICATION_NAME` | no | default `finrl-trader`; shows in `pg_stat_activity` |

`PORT` is supplied by Railway — do not set it manually.

## 3. Deploy

1. Railway dashboard → **New Project → Deploy from GitHub repo** → select this repo.
   Service settings → **Root Directory must stay empty (repository root)** — a
   subdirectory root hides `Dockerfile.trader` from the build and reproduces the
   "couldn't locate the dockerfile" error.
2. Railway reads `railway.toml` (`builder = "DOCKERFILE"`,
   `dockerfilePath = "Dockerfile.trader"`) and builds that image. Never rely on
   auto-detection: a non-standard Dockerfile name is only found via the config.
3. Add the service variables listed above. Set `DATABASE_URL` to the Neon pooled
   DSN first - `REQUIRE_DURABLE_STORE=true` is baked into the image, so a
   wrong DSN fails the deploy instead of degrading it.
4. Deploy. Check the logs for the line
   `Trader V2 on http://0.0.0.0:<port> (paper, live disabled)`.
5. Verify the health check: `https://<your-service>.up.railway.app/api/v2/health`.

`railway.toml` (config-as-code) is deprecated upstream: existing files keep
working until **2026-12-01**. Plan the migration to Railway Infrastructure as
Code before that date; the build/deploy values used here are identical.

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
2. Create (or select) the Neon PostgreSQL project and copy its **pooled** DSN
   into `DATABASE_URL`; see `docs/DEPLOYMENT_NEON.md`.
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
- Redis is optional for local development; in production the database is
  mandatory because of `REQUIRE_DURABLE_STORE`.

## 8. Troubleshooting (verified failures)

### 8.1 `couldn't locate the dockerfile at path Dockerfile.trader in code archive`

**Confirmed root cause (2026-10-26):** `railway.toml` referencing
`Dockerfile.trader` was committed in `28e5135`, but the Dockerfile itself was
only added in `255084e` (about 7 hours later). A deployment built from
`28e5135` therefore had a config pointing at a file that did not exist in that
tree. Nothing excludes the file: it is tracked at the repository root, is not in
`.gitignore`, and is not excluded by `.dockerignore`.

Checklist when this error appears again:

```bash
git rev-parse HEAD                      # deployed commit must contain the file
git cat-file -e HEAD:Dockerfile.trader  # exit 0 = present in that commit
git ls-remote origin refs/heads/main    # remote tip == the commit Railway builds
```

1. The failing deployment must be **newer** than the commit that added the file;
   redeploy from the tip of `main` instead of re-running the old deployment.
2. In the Railway service settings, `Root Directory` must be **empty / repository
   root**. A subdirectory root is the other common cause of this error, because
   `Dockerfile.trader` then falls outside the code archive. Root Directory cannot
   be set from `railway.toml` and has to be checked in the dashboard.
3. `railway.toml` must stay valid for Railway's config-as-code schema
   (`restartPolicyType` upper-case; no `[deploy.environment]` table, which
   Railway silently ignores) - `trader/tests/test_deploy_config.py` enforces
   this, the referenced Dockerfile path, and that the file is tracked by git.

### 8.2 `psycopg2.OperationalError: ... Network is unreachable` on connect

**Current store: Neon.** Use the **pooled** `-pooler` host from the Neon console;
it publishes A/AAAA records (verified: 3 IPv4 + 3 IPv6) and is reachable from
Railway. Keep `?sslmode=require`. Full runbook: `docs/DEPLOYMENT_NEON.md`.

**Historical finding (Supabase, kept for rollback context):** the Supabase
**direct** host `db.<project-ref>.supabase.co` publishes **only an IPv6 (AAAA)
record** and Railway has no IPv6 egress. Verified on the project DSN used here:
the host resolves to a single `2406:...` AAAA address and has no `A` record.

Fix the DSN **before** (or together with) the first successful deploy: the image
bakes `REQUIRE_DURABLE_STORE=true`, so an unreachable database fails the deploy
and the container restarts until the DSN is corrected.

Use the **pooled** DSN from Neon Console -> Connect -> *Connection pooling*:

```
postgresql://<user>:<password>@ep-<id>-pooler.<region>.aws.neon.tech/<db>?sslmode=require
```

- Percent-encode special characters in the password (`@`, `#`, `:`, `/`).
- The direct (unpooled) host is for migrations only; the app uses the pooler.
- Keep `REQUIRE_DURABLE_STORE=true`; the service must fail to start rather than
  fall back to in-memory storage. `trader/data/db_store.py` refuses to degrade
  when that flag is set and appends a pooler hint to the startup error.
- Legacy rollback: the Supabase Session Pooler DSN remains documented in
  `docs/DEPLOYMENT_NEON.md`; do not delete the Supabase project.