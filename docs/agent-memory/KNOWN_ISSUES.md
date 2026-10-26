# KNOWN_ISSUES.md — Bugs, Failed Attempts, Workarounds

> Record exact error + root cause + workaround. Update when fixed with commit/verification.

## Crypto / data (from code audit 2026-10-14, unexecuted — needs reproduction)
1. `finrl/meta/env_cryptocurrency_trading/env_multiple_crypto.py:114-120` — `_generate_action_normalizer` uses `len(str(price))-7`, fragile for prices <1 or large floats. `get_state():103-109` ignores `price_array`. Old 4-tuple `step()` (not Gymnasium 5-tuple), no `observation_space`/`action_space` → incompatible with SB3 directly. Workaround: write Gymnasium wrapper; add tests.
2. `finrl/meta/env_cryptocurrency_trading/env_btc_ccxt.py:67-115` — hardcoded `2**-N` scaling for 7 indicators; `draw_cumulative_return:136-179` duplicate xlabel, hardcodes `/1e6`; `load_data:195-218` brittle slicing; hardcoded `mid1=172197,mid2=216837`. Workaround: parameterize scaling, remove hardcodes.
3. `finrl/meta/data_processors/processor_ccxt.py:24-77` — inner `min_ohlcv` ignores `period` (always `1m`); non-`1m` date stepping fixed at 1440; no retry/rate-limit. Workaround: fix period handling + backoff, validate with 1-symbol fetch.
4. `finrl/applications/cryptocurrency_trading/__init__.py` — empty; stray `actor.pth` + `recorder.npy` with no script. No runnable crypto pipeline. Workaround: new example wired to fixed env/processor.
5. `finrl/meta/paper_trading/common.py:635-684` — `DRL_prediction` expects stock-env attrs (`amount`, `price_ary`, `day`, `stocks`) → unusable for `CryptoEnv`/`BitcoinEnv`.

## Stock / paper (from audit, needs reproduction)
6. `finrl/meta/data_processors/processor_alpaca.py:432,437` — `fetch_latest_data` references `bars` where `barset` intended (`if "level_1" in bars`, `barset=bars[...]`). Likely `NameError`. Needs repro + minimal fix + regression test.
7. `finrl/main.py:33-41,58-137` — train/test/trade hardcode `StockTradingEnv` + `DOW_30_TICKER`; no crypto mode. Expected until M3–M5.

## Config / secrets
8. `finrl/config_private.py` contains placeholder `xxx`; no `.env*` exists. Never commit real keys. Create `.env.example` in later milestone (not yet done).

## Failures / attempts
- None yet (M0 created memory/skills only, no code changes, no test runs).

## New-platform bugs found with live `.env` (all fixed, regression-covered)
8. `backend/app/data/providers.py` — `TimeFrame.Minute * 15` raises
   `TypeError` on alpaca-py 0.44 (`TimeFrame` has no `__mul__`). Fixed with
   `TimeFrame(15, TimeFrameUnit.Minute)`. Found via live BTC/USD fetch.
9. `backend/app/core/config.py` — user `.env` uses `.../v2` suffixed paper URL;
   alpaca-py appends `/v2` itself → `.../v2/v2/account` 404. Fixed by stripping
   trailing `/v2` in `load_settings()`. Covered by `test_config.py`.
10. `backend/app/api/server.py` — request models defined inside `create_app()`
    became `ForwardRef` → 422 on POST `/api/orders`, `/api/backtest`.
    Fixed by moving `BacktestReq`/`OrderReq` to module level.

## Deployment / operations gaps (2026-10-20)
11. **Risk kill switch is process-local.** `trader/risk/guard.py` keeps `_kill` in
    memory only. On Railway a restart/restart-policy trigger clears the kill
    state. Mitigation in place: every order is re-validated against the risk
    limits and the Alpaca paper account is the source of truth. Durable
    kill-state persistence is still OPEN.
12. **Session error logs are ephemeral on Railway.** `trader/core/kit.py`
    writes session logs under `errors/` inside the container filesystem.
    Diagnostics only — not trading state — but they do not survive a redeploy.
13. **`finrl/config_private.py` was tracked** in the first commit
    (placeholder values only, length 3). It has been untracked locally
    (`git rm --cached`). If real values are ever added, rotate the credentials.
14. **Cloudflare Pages needs the `_redirects` asset rewrites.** The dashboard
    HTML references `/static/app.js`, `/static/styles.css`, `/static/config.js`
    (FastAPI static mount). `trader/frontend/_redirects` maps them to the site
    root. Without that file the deployed page loads no CSS/JS.

## Production incident (2026-10-21) — OPEN
15. **Railway service returning HTTP 502.** `https://kala-lund-production.up.railway.app`
    answered 200 earlier the same day, then 502 on 5/5 probes (`root /` also 502).
    Railway's edge body: `{"status":"error","code":502,"message":"Application failed to respond"}`.
    **Mechanism (reproduced locally):** with `REQUIRE_DURABLE_STORE=true` and an
    unreachable/incorrect `DATABASE_URL`, `DBStore._init_db` raises, `create_app()`
    fails and `scripts/run_trader.py` exits 1 — the container never serves. This is
    the designed fail-closed behaviour; the trade-off is that a DB misconfiguration
    becomes a total outage instead of a degraded service.
    **Likely trigger:** `DATABASE_URL` + `REQUIRE_DURABLE_STORE=true` were added after
    the healthy check, and the configured DSN does not connect from Railway
    (Supabase direct connections are IPv6-only; use the **pooler** URL, or verify the
    password/port). **Next step:** Railway dashboard -> service -> Deployments -> Logs,
    look for `Durable store required but the PostgreSQL connection failed` or
    `Blocking configuration errors:`. Not resolvable from this machine (Railway CLI
    unavailable).
16. **Broker read latency was unbounded (FIXED 2026-10-21).** alpaca-py never passes a
    `timeout` to `requests.Session`, so a hung broker connection blocked API request
    threads indefinitely. `BROKER_READ_TIMEOUT_SECONDS` (default 10) now bounds
    read-only broker calls. Order submission is intentionally still unbounded (a
    timeout there would create ambiguous order state, which the kill-switch path
    handles explicitly).
17. **Railway build failed: `couldn't locate the dockerfile at path Dockerfile.trader in code archive` (FIXED in config 2026-10-26, redeploy pending).**
    **Root cause:** `railway.toml` (added in `28e5135`, 2026-10-20 20:48) already referenced
    `Dockerfile.trader`, but that file was only added later in `255084e` (2026-10-21 04:07).
    The failed deployment was built from the older tree, so the referenced Dockerfile was not
    part of the archive. Nothing excluded it: it is tracked at the repo root, not in
    `.gitignore`, not in `.dockerignore`. `origin/main` is now `255084e`, so the file IS in the
    deployed commit - a fresh deployment from the tip of `main` finds it.
    **Secondary defects found while verifying:** `restartPolicyType = "on_failure"` (Railway
    schema allows only `ON_FAILURE`/`ALWAYS`/`NEVER`) and a `[deploy.environment]` table
    (Railway cannot set service variables from config-as-code; those values were silently never
    applied, including `REQUIRE_DURABLE_STORE=true`). Fixed: schema-valid `railway.toml` with
    `builder = "DOCKERFILE"`, and the safety env vars are now baked into `Dockerfile.trader`.
    **Guard:** `trader/tests/test_deploy_config.py` fails CI if the config references a missing
    or untracked Dockerfile, uses an invalid restart policy, or drops the paper-only/durable-store ENV values.
    **Still to check manually:** service `Root Directory` must be empty (repo root) - it is a
    dashboard-only setting and a subdirectory root produces the same archive error.
    **Also:** Railway config-as-code is deprecated upstream (existing files work until 2026-12-01);
    plan the migration to Infrastructure as Code. Ordering matters for the next deploy: fix
    `DATABASE_URL` (see 18) **before** redeploying, because the image now enforces
    `REQUIRE_DURABLE_STORE=true` and an unreachable DB fails the deploy instead of degrading.
18. **Supabase `Network is unreachable` on startup (DIAGNOSED 2026-10-26, fix = DSN change).**
    **Root cause:** `DATABASE_URL` points at the Supabase **direct** host
    `db.<project-ref>.supabase.co`, which publishes **only an IPv6 AAAA record**
    (verified for this project: single `2406:...` AAAA address, no `A` record - exact
    value recorded in TEST_HISTORY). Railway has no IPv6 egress by
    default, so `psycopg2` raises `Network is unreachable` / `timeout expired`.
    Reproduced locally: `psycopg2.connect(direct_dsn)` -> `OperationalError ... (2406:da1c:...), port 5432 failed`.
    **Fix:** use the Supabase **Session Pooler** DSN
    `postgresql://postgres.<project-ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres`
    (username must contain the project ref; percent-encode the password). Keep
    `REQUIRE_DURABLE_STORE=true`; `trader/data/db_store.py` already refuses to fall back to memory
    and appends the pooler hint to the startup error. Alternative (not preferred): Railway
    `deploy.ipv6EgressEnabled = true`, which only exists on the official config schema.
    Also note: `.env` contains `DATABASE_URL` twice (both direct host) - the last one wins; clean up
    when switching to the pooler.
## Neon migration + dashboard audit (2026-10-26)

19. **RESOLVED - Supabase direct host unreachable / Railway 502 (items 15 & 18).**
    **Fix:** durable store moved to Neon's pooled endpoint (IPv4 + IPv6 A records
    verified) with `sslmode=require`. `REQUIRE_DURABLE_STORE=true` stays in the
    image. Schema v1 applied and verified with `scripts/db_migrate.py`
    (`schema_ok=True`, four tables, zero rows). Supabase is **not** deleted or
    overwritten; its DSN remains as a commented rollback line.
    **Still unverified:** an actual Railway -> Neon connection (no deploy performed).
20. **Test suite could reach the network / real Alpaca keys (FIXED 2026-10-26).**
    **Root cause:** `trader/tests/conftest.py` blanked `DATABASE_URL`/`REDIS_URL`
    only. The developer `.env` supplied `APCA_*` credentials, and Alpaca crypto
    bars are readable **without** credentials, so `/api/v2/positions` and
    `/api/v2/quotes` returned `available=true` inside hermetic tests.
    **Fix:** blank every Alpaca alias, force `DATA_PROVIDER=alpaca`, and
    monkeypatch `Broker.account` / `Broker.positions_detail` / `MarketStore.many`
    in the tests that assert unavailable states. No test now performs I/O.
21. **Dashboard password could leak through the cancel URL (FIXED 2026-10-26).**
    **Root cause:** `/api/jobs/{jid}/cancel` read `password` from the query string,
    so it could appear in proxy logs and browser history.
    **Fix:** the endpoint now reads the password from the JSON body
    (`Body(default=None)`); the dashboard sends it in the POST body. A regression
    test asserts the query-string form is rejected (403) and the body form works.
22. **Frontend had no Cloudflare Pages security headers (FIXED 2026-10-26).**
    **Fix:** `trader/frontend/_headers` sets CSP (`script-src 'self'`),
    `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`,
    `Permissions-Policy`, and cache rules. `trader/tests/test_frontend_assets.py`
    guards the CSP, `_redirects` mapping, absence of inline scripts/handlers,
    `node --check`, and the build output.
23. **Order fills had no timestamp (FIXED 2026-10-26).** `Fill` now carries a
    `ts` UTC ISO-8601 field set at submission, so the dashboard "Execution
    history" time column is populated instead of rendering an em dash.
24. **Review findings fixed before final verification (2026-10-26).**
    - **Order outcome vs persistence:** `/api/orders` saved the order before
      handling `status == "unknown"`. With `REQUIRE_DURABLE_STORE=true` a DB blip
      raised first, returning HTTP 400 and skipping `risk.trip(...)`. Fixed: trip
      the kill switch first, then persist best-effort and report
      `persistence_error`; a persistence failure never rewrites the execution
      verdict. Regression tests cover unknown and filled outcomes.
    - **X-Forwarded-For spoofing:** the rate limiter trusted the client header, so
      rotating it gave unlimited password guesses. Fixed with
      `TRUST_PROXY_HEADERS` (default `false`; right-most hop when enabled) and a
      bounded rate map. Railway should set it to `true`.
    - **Readiness could stay green with a dead DB:** `/api/v2/health` used the
      query-free `db.stats()`. It now probes with `db.health_check()` behind the
      5 s health cache.
    - **Data errors treated as connection loss:** `DBStore._write` reconnected on
      any exception and could tear down a healthy pooled connection. It now
      retries only `OperationalError`/`InterfaceError`/`ConnectionError`; data or
      constraint errors raise immediately.
    - **Insecure TLS passthrough:** remote `sslmode=disable/allow/prefer` is now
      refused at startup (`DBStore.__init__`).
    - **DSN redaction gap:** `safe_msg` only redacted on keywords; it now strips
      embedded `scheme://user:pass@` credentials. `scripts/db_migrate.py` uses it.
    - **CSP breadth:** the Pages build now pins `connect-src` to the configured
      API origin instead of allowing every HTTPS host.
    - **Dashboard lock mismatch:** when `DASHBOARD_PASSWORD` is unset the backend
      locks operations, but the UI used to report "unlocked". The UI now keeps
      controls locked and explains how to enable them.

## 2026-10-26 (later) - Broker read starvation + durable-store reporting (fixed)

- **"Paper account unreadable" (`Broker read 'account' exceeded 10.0s`).**
  Root cause: alpaca-py never passes a `timeout` to `requests.Session.request`
  (`alpaca/common/rest.py::_one_request`). `Broker._read` bounded the *future*,
  not the socket, and `Future.cancel()` cannot stop a blocked thread. Four slow
  calls therefore occupied all four read workers permanently, so every later
  read timed out even after the network recovered. Each `_c()` call also built a
  new `TradingClient` (new TLS handshake), which is enough on its own to exceed
  a 10 s bound on a cold connection.
  Fixed in `trader/execution/broker.py`: per-request `(connect, read)` timeout
  injected into the client session (10 s for reads, 30 s for submits - both
  bounded, submissions longer so an order is not abandoned into ambiguous
  state); the read pool is retired and replaced after a timeout; the client is
  memoized per thread (keeps the TLS connection); account verification and
  order-status reads are bounded by that same socket timeout.
  Measured locally (paper, read-only): first `account()` 1.9 s, subsequent
  0.26-0.30 s.
- **Diagnostics contradiction (`backend=memory`, `durable=false`, `ok=true`).**
  Root cause: `/api/v2/db` took the `stats()` snapshot *before* `verify()`
  (which reconnects) and `stats()` derived `backend` from the live handle, so a
  dropped idle pooler connection was reported as "memory" while the probe said
  postgres. `get_open_orders()` also invalidated the handle without setting
  `_degraded`, leaving "not durable but not degraded".
  Fixed: `stats().backend` now names the *configured* store, with new
  `connected`/`fallback_memory` fields; `verify()` runs first and the verdict is
  `ok = schema_ok and durable`; a connection-class read failure marks the store
  degraded while data/constraint errors keep the pooled connection.
- **Unsafe execution without durable storage.** `/api/orders` submitted first
  and persisted afterwards; `Loop.once` swallowed `save_order` failures. Both
  now refuse/halt *before* submission when `REQUIRE_DURABLE_STORE` is set and
  `DBStore.durable_available()` fails; a submitted-but-unrecorded order trips
  the kill switch (API and automation paths both, immediately before each send).
- **Health semantics.** `/api/v2/health` now returns 503 when the instance is
  not ready (invalid config, or required durable store unavailable); `/health`
  and `/healthz` do the same. An engaged kill switch keeps HTTP 200 (body
  `ok=false`, `status=degraded`) so an orchestrator restart cannot clear it.
