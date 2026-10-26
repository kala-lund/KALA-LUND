# NEXT_STEPS.md — Pending Tasks, Priorities, Next Actions

## V2 REWRITE (2026-10-16, user request: clean production rebuild) — DONE
New package `trader/` + rebuilt UI. Old `backend/` kept as untouched fallback.
- [x] core, data (concurrent fetch), features + 6 strategies, engine, risk, execution, backtest, api, UI
- [x] 13 tests green; live smoke on :8001 (33 symbols, 33 signaled, 14.3s, 1 BUY)
- [x] `start.bat` now runs V2 (`scripts/run_trader.py`)
- KNOWN V2 GAPS: RL proposals not wired (SB3 present only); Muse fail-closed as before;
  submit throttle 10/min; vanilla UI (no React build step by design).

## Priority order
1. [ ] M1: Verify baseline — `python -m pip list`, `pytest unit_tests -q`, `python -m finrl --help`, import `CryptoEnv`/`BitcoinEnv`/`CCXTEngineer`. Record in TEST_HISTORY.
2. [ ] M1: Confirm scope with user: venue (Alpaca crypto paper?), universe/timeframe, Muse required for MVP?
3. [ ] M2: Crypto data validation — 1-symbol CCXT fetch + indicators, check timestamps/no look-ahead.
4. [ ] M3: Gymnasium wrapper for crypto env + fee/slippage backtest + SB3 smoke train.
5. [ ] M4: Risk guards (position/exposure/order/stale-data/loss-limit/kill-switch) + unit tests.
6. [ ] M5: Alpaca paper validation (account, crypto symbols, dry-run order).
7. [ ] M6: Muse signal-review (configurable, timeout, fallback) — only if user confirms.
8. [ ] M7: Dashboard (status/portfolio/signals/backtest/orders/risk/logs/stop, protected ops).
9. [ ] M8: Release check (tests + config + security).

## Exact next actions (do in order)
1. Run: `python --version && pip show finrl 2>&1 | head -20` (or `pip list`).
2. Run: `pytest unit_tests -q` (per `setup.cfg` pattern `test_*.py`).
3. Run: `python -c "from finrl.meta.env_cryptocurrency_trading.env_multiple_crypto import CryptoEnv; print('ok')"` and same for `env_btc_ccxt:BitcoinEnv`, `processor_ccxt:CCXTEngineer`.
4. Ask user scope questions (venue/universe/Muse-MVP?) via question tool.
5. Update MEMORY.md + TEST_HISTORY.md with results before starting M2.

## Deployment readiness (2026-10-20) - DONE in code, MANUAL steps left
Code complete: GitHub CI (`.github/workflows/ci.yml`), Railway config
(`railway.toml` + `Dockerfile.trader`), Cloudflare Pages config
(`wrangler.toml` + `scripts/build_frontend.js`), docs (`README.md`,
`docs/DEPLOYMENT_RAILWAY.md`, `docs/DEPLOYMENT_CLOUDFLARE.md`).

## Railway redeploy unblock (2026-10-26) - code fixed and committed, deploy pending
Fixed in the repository (not deployed yet):
`railway.toml` is now schema-valid (`builder = "DOCKERFILE"`,
`restartPolicyType = "ON_FAILURE"`, invalid `[deploy.environment]` removed);
`Dockerfile.trader` bakes `REQUIRE_DURABLE_STORE=true` + `PYTHONUNBUFFERED=1`;
`trader/tests/test_deploy_config.py` guards the config in CI;
`docs/DEPLOYMENT_RAILWAY.md` documents both failures.

Owner actions, in order:
1. [ ] Confirm the deployed commit contains `Dockerfile.trader` (`git cat-file -e
       HEAD:Dockerfile.trader`) - the previous failure was a deployment built from
       a commit that predates the file.
2. [ ] Railway service settings -> `Root Directory` must be **empty** (repo root).
       A subdirectory root reproduces the `code archive` error.
3. [ ] Railway -> service variables -> set `DATABASE_URL` to the Supabase
       **Session Pooler** DSN
       (`postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres`).
       The direct `db.<ref>.supabase.co` host is IPv6-only and unreachable.
4. [ ] Confirm the service has `REQUIRE_DURABLE_STORE=true` (now baked into the
       image; a dashboard value would only override it).
5. [ ] Deploy once, then check logs for `Trader V2 on http://0.0.0.0:<port>` and
       `GET /api/v2/health` -> 200 with `components.db.detail = "postgresql"`.
       Do not loop retries on failure; read the log line first.

Ordering constraint: steps 3 and 4 must be done **before** step 5. The image now
enforces `REQUIRE_DURABLE_STORE=true`, so an unfixed/incorrect `DATABASE_URL`
makes the deploy fail (healthcheck never passes) instead of silently degrading.

Manual steps that require the owner (dashboard access / DNS):
1. [ ] Create the Railway service and add the variables listed in
      `docs/DEPLOYMENT_RAILWAY.md` (`DATABASE_URL`, `CORS_ORIGINS`,
      `DASHBOARD_PASSWORD`, Alpaca paper keys).
2. [ ] Create the Cloudflare Pages project (build command
      `node scripts/build_frontend.js`, output `dist`, env `API_BASE_URL`).
3. [ ] Custom domain + DNS (CNAME to `<project>.pages.dev`); HTTPS is automatic.
4. [ ] Update Railway `CORS_ORIGINS` with the final frontend origin.
5. [ ] Put the read-only dashboard endpoints behind Railway deployment
      protection or Cloudflare Access before public exposure
      (`/api/v2/summary`, `/api/v2/orders`, `/api/v2/risk` are not password gated).
6. [ ] Review the staged diff and decide on the commit message.

Open engineering items (not started):
- [ ] Durable kill-switch state (currently process-local - a restart clears it).
- [ ] Replace the upstream hardcoded Shioaji / FRED demo keys with env lookups
      and rotate them (they are public in upstream FinRL since 2023).
- [ ] Upstream `unit_tests` are not part of CI yet (full FinRL dependency set).

## Neon + dashboard migration (2026-10-26) - code done, deploy pending

Completed locally (no commit/push/deploy):
- Neon pooled `DATABASE_URL` active with `sslmode=require`; `REQUIRE_DURABLE_STORE=true`.
- `scripts/db_migrate.py` applied/verified schema v1 (4 tables, 0 rows).
- `DBStore` SSL/retry/verify/row-count support; `/api/v2/db` + `/api/v2/health`.
- Dashboard rebuilt (8 views, themes, SVG charts, honest unavailable states).
- Cloudflare `_headers`, `_redirects`, frontend asset tests.
- Docs: `.env.example`, `railway.toml`, `docs/DEPLOYMENT_RAILWAY.md`,
  `docs/DEPLOYMENT_NEON.md`.

Owner actions, in order (all require explicit approval; none were executed):
1. [ ] Review `git diff` and run the full verification block from TEST_HISTORY
       (trader tests, unit_tests, frontend build, API smoke, Docker build).
2. [ ] Railway -> Variables: set `DATABASE_URL` to the Neon **pooled** DSN with
       `?sslmode=require`; keep `REQUIRE_DURABLE_STORE=true`; set `CORS_ORIGINS`
       to the exact Pages origin, `TRUST_PROXY_HEADERS=true`, and a strong
       `DASHBOARD_PASSWORD`.
3. [ ] Deploy backend; verify `/api/v2/health` -> `components.db.ok=true` and
       `/api/v2/db` -> `schema_ok=true`, `version=1`.
4. [ ] Rebuild Pages with `API_BASE_URL=https://<railway-origin> npm run build:pages`
       and deploy `dist/`.
5. [ ] Rotate the Neon role password if any DSN was ever pasted into chat/logs;
       update the Railway variable afterwards.
6. [ ] Only after a successful deploy: consider a Neon restore-point/branch
       backup and a Railway Infrastructure-as-Code migration (config-as-code is
       deprecated 2026-12-01).
