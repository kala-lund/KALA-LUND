# TASK CHECKLIST — REBUILD MISSION (Phase K)

Persistent checkpoint for interruption resilience. Resume from last verified item.

## Status
- Repo: upstream FinRL 0.3.8 preserved untouched + `trader/` V2 (clean rewrite) + `backend/` V1 fallback
- V2 tests: **155 passed / 2 skipped** (`trader/tests`, 2026-10-26); upstream `unit_tests`: 78 passed, 2 pre-existing Alpaca 401 failures
- Durable store: Neon pooled PostgreSQL, schema v1 verified (`scripts/db_migrate.py`)
- Dashboard: rebuilt 8-view vanilla JS/CSS/SVG terminal, CSP + Pages `_headers`
- start.bat → scripts/run_trader.py (V2)
- Agents: 9 subagents created (.opencode/agents/)
- No commit/push/deploy performed; Railway + Cloudflare steps remain manual

## Phase A — Repository Audit [COMPLETE]
- [x] Map all source files/modules/configs/deps/entry points
- [x] Identify duplicates/dead code/circular imports/broken imports
- [x] Audit every external API call (timeout, retry, rate-limit, validation, error handling)
- [x] Find blocking/sequential/repeated/excessive-polling operations
- [x] Inspect strategy logic (look-ahead, leakage, candle alignment, fills, accounting)
- [x] Inspect auth/secrets/CORS/input validation/filesystem/subprocess/dependency risks
- [x] Run existing tests, record baseline failures (44/44 green V2, 28/28 green V1)
- [x] Produce audit report (severity, files, root causes, migration sequence)

## Phase B — Clean Architecture [COMPLETE]
- [x] Define module boundaries (frontend/backend/config/providers/features/strategies/ai/risk/backtest/broker/db/logging)
- [x] Eliminate circular deps, duplicated logic, hidden global state (V2 import graph acyclic)
- [x] Typed interfaces, explicit config, structured errors, DI where appropriate
- [x] FastAPI backend + vanilla JS frontend decision (no React build step)

## Phase C — API Speed & Reliability [COMPLETE]
- [x] Centralized API-client layer (timeouts, retries w/ backoff+jitter, Retry-After, rate limits, circuit breaker, concurrency limits, dedup, bounded cache, pagination, schema validation)
- [x] Safe shutdown/cancellation
- [x] Structured latency/error metrics
- [x] No blind order retries; reconcile broker state first

## Phase D — Professional Web Dashboard [COMPLETE]
- [x] Frontend↔V2 contract consistent (last→last_run, equity_curve→curve, strategy→name, confidence→conf)
- [x] 9 screens: Overview, Markets, Strategy Lab, Backtesting, Paper Trading, Portfolio/Risk, AI Research, System Health, Settings
- [x] Loading/empty/stale/offline/error states; no dead buttons; no credentials in UI

## Phase E — Quantitative Strategy Engine [COMPLETE]
- [x] Fixed backtest bugs (ZeroDivisionError at zero price, math.log domain error on negative equity, artificial per-bar churn, Sharpe ppy parameterized)
- [x] Momentum, mean-reversion, volatility sizing, regime detection, optional RL behind stable interface
- [x] Configurable thresholds, explicit HOLD/NO-TRADE
- [x] Data validation (missing candles, dups, invalid prices, timestamps, order)
- [x] Look-ahead/leakage prevention; chronological splits; walk-forward
- [x] Realistic fees/slippage; metrics (return, MDD, Sharpe, vol, turnover, win rate, profit factor)

## Phase F — Risk Engine & Paper Execution [COMPLETE]
- [x] Added notional <= buying power check
- [x] Submit failures reconciled, never assumed as fills
- [x] Per-symbol skip instead of global halt
- [x] Configurable limits (order notional, position size, exposure, daily loss, drawdown, buying power, symbol validation, stale data, dups, reconciliation, order-state tracking, rate limits, safe retries, emergency stop)
- [x] Fail-closed; defaults disabled until explicitly enabled; paper-only guard
- [x] Emergency stop blocks new orders immediately

## Phase G — Security Hardening [COMPLETE]
- [x] Authz on read endpoints (/api/positions, /api/logs)
- [x] Rate limiting on password-checked endpoints (/api/orders, /api/kill, /api/automate/*)
- [x] Traceback redaction in unhandled exception logs
- [x] Secret redaction by value pattern (sk-, pk_, PKE, long alnum) not just key hint
- [x] .env.example sanitized; localhost default binding

## Phase H — Automated Testing [COMPLETE]
- [x] Backtest correctness/crash tests (zero price, negative equity, churn, drawdown, Sharpe)
- [x] Muse API adapter mocked tests (success, timeout, malformed, rate-limit)
- [x] Alpaca paper adapter mocked tests (order sub/reconcile/cancel/dup)
- [x] Frontend JS syntax-checked (no live browser test framework configured)
- [x] Security tests (unauthorized, oversized inputs, path traversal, CORS, authz)
- [x] Run lint/type/format; document unrunnable tools

## Phase I — Performance & Recovery [IN PROGRESS]
- [ ] Measure startup time, endpoint latency, external API latency/errors, dashboard refresh, DB latency, memory, test time
- [ ] Health/readiness endpoints, graceful shutdown, structured logs, outage recovery
- [ ] Bounded storage/retention; no per-candle high-volume logging
- [ ] Backup/restore docs

## Phase J — Delivery & Acceptance [PENDING]
- [ ] Working backend + responsive dashboard + validated integrations
- [ ] dependency lockfile/spec; .env.example sanitized; README with Windows commands; dev/prod startup commands; troubleshooting; API docs; architecture/security docs
- [ ] Test report (commands, pass/fail, blockers); performance comparison (measured)
- [ ] Final acceptance gate

## Phase K — Multi-Agent Team [COMPLETE]
- [x] Agents created: finrl-researcher, quant-strategist, backtest-engineer, trading-risk-reviewer (+ existing 5)
- [x] Phase A audits delegated in parallel (all complete)
- [x] Phase B-J implementation delegated and executed
- [ ] QA agent verifies integrated application
- [ ] Security auditor independent final review

## Notes
- .env: gitignored, contains live Alpaca paper keys — NEVER commit, rotate manually if exposed
- Paper trading ONLY; ENABLE_LIVE_TRADING=false enforced by code
- All monetary figures PAPER/simulated, never real
