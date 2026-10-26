# MEMORY.md — Index of Project Knowledge

> Read this + relevant linked files at start of every substantial task. Keep concise.

## Current status (2026-10-26, verified)
- Active app: `trader/` (FastAPI) + `trader/frontend/` static dashboard. `backend/`/`frontend/` are legacy fallbacks, untouched.
- Durable store: **Neon PostgreSQL** (`production` branch), schema v1 applied and verified; legacy Supabase kept as rollback (not deleted, no data copied).
- Dashboard: 8 views, dark/light themes, real backend data with explicit unavailable states, SVG charts, CSP-safe (external scripts only).
- Tests: `trader/tests` 155 passed / 2 skipped. Upstream `unit_tests`: 78 passed / 1 skipped / 3 xfailed, 2 pre-existing Alpaca 401 failures (hardcoded `???` credentials in an untouched test). See TEST_HISTORY.
- Live trading remains prohibited; paper only. No commit/push/deploy performed by the agent.

## Historical status (2026-10-14, kept for context)
- Repo: upstream FinRL 0.3.8 preserved untouched; new `backend/` + `frontend/` platform alongside it.
- Entry: `python scripts/run_local.py` → FastAPI on http://127.0.0.1:8000 (dashboard `/`, API `/api/*`).
- Working (verified): config validation, health, signals pipeline (mock/offline paths), 6 strategies,
  Gymnasium RL env + SB3 status, Muse fail-closed adapter, risk engine + kill switch, backtest +
  walk-forward, paper-guarded orders, dashboard (9 tabs), 13 automated tests green.
- Missing/untested: live market fetch (no Alpaca keys yet), Muse live call, RL training run, React frontend
  (static SPA serves instead, offline-safe).

## Map
- `docs/agent-memory/PROJECT_CONTEXT.md` — goals, architecture, deps, status.
- `docs/agent-memory/DECISIONS.md` — decisions + reasons.
- `docs/agent-memory/KNOWN_ISSUES.md` — bugs with evidence.
- `docs/agent-memory/NEXT_STEPS.md` — pending tasks + next actions.
- `docs/agent-memory/TEST_HISTORY.md` — verification evidence.
- `AGENTS.md` (repo root) — durable agent rules.
- `.opencode/skills/*/SKILL.md` — 12 reusable workflows (auto-discovered).
- `.opencode/agents/*.md` — 5 subagents: code-reviewer, quant-researcher, risk-reviewer, security-reviewer, python-builder.
- `.opencode/opencode.json` — project config (skill permissions allow).

## Confirmed facts vs assumptions
- Confirmed: file inventory and crypto gaps above (see PROJECT_CONTEXT, audit 2026-10-14).
- Assumption (unverified): user wants Alpaca crypto paper trading + Muse signal review + dashboard. Confirm before building.
- Hypothesis: `processor_ccxt.py`, `CryptoEnv`, `BitcoinEnv` need fixes before reuse — see KNOWN_ISSUES.

## Last change
- 2026-10-14: Created AGENTS.md + memory system + 12 skills. No source code modified. No tests run yet.
- 2026-10-14: Added `.opencode/agents/` (5 subagents) + `.opencode/opencode.json` per V2 docs (skills auto-discovered under `.opencode/skills`, agents under `.opencode/agents`). No source code modified.
- 2026-10-18: Phase 1 P0 broker safety implemented across V1, V2, and legacy Alpaca order boundaries. Exact paper endpoint, explicit paper environment, client endpoint identity, non-empty credentials, and ACTIVE account verification are required immediately before submission. No external API calls or orders were made; see TEST_HISTORY for targeted evidence.
- 2026-10-19: Phase 2 execution P1 hardening added startup config rejection, shared client-order IDs, unknown/rejected execution fail-closed behavior, pre-start open-order reconciliation, backend buying-power validation, side-aware quantity-based take-profit exits with deduplication, broker-derived daily P&L, and explicit default-deny legacy risk gates. No external trading calls or orders were made; see TEST_HISTORY.
- 2026-10-26: Railway deploy fix. Confirmed the `couldn't locate the dockerfile at path Dockerfile.trader in code archive` failure came from a deployment built off `28e5135` (config referenced the Dockerfile ~7h before `255084e` added it; `origin/main` now has it). Made `railway.toml` schema-valid (`builder = "DOCKERFILE"`, `ON_FAILURE`, dropped the unappliable `[deploy.environment]` table) and baked `REQUIRE_DURABLE_STORE=true` into `Dockerfile.trader`. Added `trader/tests/test_deploy_config.py` as a CI guard. Diagnosed the DB blocker: `DATABASE_URL` uses the IPv6-only Supabase direct host (only AAAA record) -> Session Pooler DSN required. Nothing committed, pushed, or deployed; no secrets printed.
- 2026-10-26: Migrated durable storage to Neon. `.env` now points `DATABASE_URL` at the Neon pooled host with `sslmode=require` (Supabase line kept commented for rollback); `REQUIRE_DURABLE_STORE=true`. Added `scripts/db_migrate.py` (additive schema + verify, exit 0/1/2) and ran it: schema v1, four tables, zero rows. `DBStore` gained SSL detection, bounded connect retries, `application_name`, `row_counts()`, `verify()`, reconnect-on-health and serialized writes. See `docs/DEPLOYMENT_NEON.md`.
- 2026-10-26: Dashboard rebuilt (`trader/frontend`): 8 views (Overview, Portfolio, Markets, Strategies, Backtest, Risk, Activity, Settings), dark/light themes, responsive nav, SVG equity/drawdown/benchmark/candle charts, allocation, health, explicit loading/empty/offline/error states. Password stays in memory; cancel password moved to POST body. Added Cloudflare `_headers` (CSP `script-src 'self'`) and frontend build/asset regression tests.
