# DECISIONS.md — Decisions, Alternatives, Reasons

| Date | Decision | Alternatives considered | Reason |
|---|---|---|---|
| 2026-10-14 | Preserve upstream FinRL, extend incrementally for crypto | Rewrite as new framework (like FinRL-X) | User instruction: preserve functionality, modify instead of replacing |
| 2026-10-14 | Paper trading only; live trading prohibited | Allow live flag | Safety: no risk controls, no verified execution path; README disclaimer is academic-use |
| 2026-10-14 | Memory split: AGENTS.md (durable rules) vs docs/agent-memory (changing knowledge) | Single file | Avoid stale rules mixing with status; per user spec |
| 2026-10-14 | 12 skills under `.opencode/skills/` matching user list | Fewer generic skills | User explicitly requested each domain; each gets workflow + validation + safety |
| 2026-10-14 | Secrets via `finrl/config_private.py` (gitignored) or env, never in memory | `.env` committed / memory storage | `.gitignore:148` already ignores `config_private.py`; avoid leak |
| 2026-10-14 | No source code changed in M0 | Fix crypto bugs immediately | First priority per prompt is understand + plan + memory/skills before code |
| 2026-10-14 | New `backend/` + `frontend/` alongside `finrl/` (not a rewrite) | Rewrite finrl/ crypto modules in place | Preserve upstream; new platform in isolated packages per spec structure |
| 2026-10-14 | Static HTML/JS dashboard served by FastAPI (no React build) | React+TS SPA with npm build | Local-first, zero build step, offline-safe canvas charts; recommended stack deferred |
| 2026-10-14 | Stdlib-only config (no pydantic-settings dependency) | pydantic-settings | Fewer deps, works on Python 3.13 immediately |
| 2026-10-14 | Muse via Anthropic Messages API, strict schema, fail-closed | Open-ended LLM output | Verified SDK 0.109.2 has `messages`; untrusted-output discipline |
| 2026-10-19 | Require one client order ID from risk approval through broker submission and block automation until open-order reconciliation succeeds | Generate a new ID at submission / start automation optimistically | Prevent duplicate orders after ambiguous submissions or restarts; fail closed on missing reconciliation |
| 2026-10-19 | Legacy Alpaca order paths require an explicit caller-supplied risk gate and default to denial | Preserve unguarded legacy submission for compatibility | Legacy V1 loops do not own the modern risk engine; default denial prevents uncontrolled paper execution |
| 2026-10-20 | Host split: FastAPI backend on Railway, static dashboard on Cloudflare Pages; backend never deployed to Workers | Move backend to Cloudflare Workers | Backend needs native wheels (psycopg2-binary, ccxt, SB3) and a long-lived process; Workers runtime is incompatible. Only the vanilla-JS/CSS frontend ships to Pages |
| 2026-10-20 | `REQUIRE_DURABLE_STORE` env flag makes startup fail when `DATABASE_URL` is missing/unreachable (enabled by default in `railway.toml`) | Always require a DB / always allow the in-memory fallback | Production order/signal state must never silently live in ephemeral memory; the flag keeps local development (no DB) working |
| 2026-10-20 | Frontend API origin is injected at build time into `dist/config.js` by `scripts/build_frontend.js` (reads `API_BASE_URL`) | Hand-edit `index.html` per environment / fetch a runtime config endpoint | No bundler or npm dependency needed; single source of truth; keeps secrets out of the frontend; Pages build env var is the only manual step |
| 2026-10-20 | New lightweight CI job (`.github/workflows/ci.yml`, Python 3.11) runs `trader/tests` + `backend/tests` with mocked brokers | Reuse the upstream docker-based `test.yml` | Upstream workflow builds the full FinRL image; the V2 app needs its own gate that never needs credentials or network |
| 2026-10-20 | `finrl/config_private.py` untracked from the git index (kept on disk) | Leave it tracked | `.gitignore` already excludes it; tracking it defeats that intent and risks committing credentials |

## Open questions (need user confirmation)
- Alpaca paper API keys not yet provided → live market/pipeline runs untested (mock/quality paths tested).
- Muse model name confirmed at runtime; default `claude-sonnet-4-5` configurable via env.
- RL algorithm choice: PPO implemented; comparison (A2C/SAC) deferred to research phase.
