# AGENTS.md — Permanent Coding Instructions

This file is the durable instruction set for all AI coding agents working in this repo.
Memory files under `docs/agent-memory/` hold changing project knowledge. This file holds rules.

## 1. Project identity
- Upstream: FinRL 0.3.8 (AI4Finance-Foundation/FinRL), classic train-test-trade framework.
- Goal extension: reliable, modular, testable AI-powered **cryptocurrency** trading system (paper-only).
- Never replace the whole repo. Preserve existing functionality; modify incrementally.
- Live trading is **prohibited**. Paper trading only.

## 2. Before changing anything
1. Read `docs/agent-memory/MEMORY.md`, `PROJECT_CONTEXT.md`, `DECISIONS.md`, `KNOWN_ISSUES.md`, `NEXT_STEPS.md`.
2. Inspect actual code (`finrl/`, `examples/`, `unit_tests/`, `finrl/config.py`). Do not trust stale memory over code.
3. Produce or update a short plan with acceptance criteria before large changes.

## 3. Python engineering rules
- Python >=3.7 compatible (repo supports 3.7–3.12). Use `from __future__ import annotations`.
- Typed, clean, maintainable code. No untyped new public functions without annotations.
- Error handling: never swallow exceptions to fake success. Log with context, re-raise or return explicit error.
- Config: never hardcode keys/secrets. Use `finrl/config_private.py` (gitignored) or env vars / `.env` (never commit). Never log secrets.
- Paths: repo-root-relative, create dirs via `check_and_make_directories`-style helpers.
- Follow `setup.cfg` flake8 `max-line-length=127`, black/isort conventions.

## 4. Quant / trading safety rules
- Validate timestamps, no look-ahead bias, train/test split strictly separated.
- Account for fees + slippage in backtests. Report them explicitly.
- Enforce: position limits, exposure limits, order validation, stale-data checks, daily loss limits, emergency stop / safe shutdown.
- Crypto envs (`env_multiple_crypto.py`, `env_btc_ccxt.py`) are legacy ElegantRL-style, not Gymnasium-compatible. Wrap/adapt before use with SB3; do not silently assume compatibility.
- Never claim profitability or that a feature works without executed verification.

## 5. API integration rules
- Validate every external endpoint: auth, schema, timeouts, retries, rate limits, response parsing.
- Alpaca: paper base URL only (`https://paper-api.alpaca.markets`). Verify account status + supported crypto symbols before orders.
- Binance/CCXT: `processor_ccxt.py` is fetch-only, has known period/rate-limit bugs. No live order path exists — do not invent one.
- Muse/LLM integration (if added): configurable model, timeout, fallback to no-op on failure. Never block trading loop on LLM.

## 6. Testing / verification rules
- Reproduce bug first, fix minimally, run regression tests.
- For new code: add/extend `unit_tests/test_*.py` (`pytest` per `setup.cfg`).
- Record every verification in `docs/agent-memory/TEST_HISTORY.md` with exact command + result.
- Security checks: no secrets in diffs, validate operational endpoints (dashboard stop/kill switches protected).

## 7. Memory rules
- Update memory on: important decision, discovery, failure, architecture change, completed milestone.
- `DECISIONS.md`: decision + alternatives + reason. `KNOWN_ISSUES.md`: exact error + root cause + workaround. `NEXT_STEPS.md`: next concrete actions.
- Keep memory concise, searchable, no duplication. No secrets, no large logs/datasets/full files.
- If code contradicts memory, fix memory after inspecting code.

## 8. Skills
- Reusable workflows live in `.opencode/skills/*/SKILL.md`.
- Invoke relevant skill(s) explicitly when task matches. Creating a skill file does not auto-activate it.
- Every skill must have: when-to-use, workflow, validation criteria, safety constraints.

## 9. Scope discipline
- Prefer editing existing files over creating new ones. Do not create files unless necessary.
- Respect active user corrections/constraints across turns until explicitly lifted.
