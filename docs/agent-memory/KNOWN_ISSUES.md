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