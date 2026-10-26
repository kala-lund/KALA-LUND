# PROJECT_CONTEXT.md — Goals, Architecture, Dependencies, Status

## 1. Goals
1. Preserve upstream FinRL research/education pipeline.
2. Incrementally build modular, testable AI-powered **crypto** trading system: data → signals (DRL + optional Muse review) → risk → Alpaca paper execution → dashboard.
3. Live trading prohibited. Paper-only with kill switches.

## 2. Architecture (actual, verified 2026-10-14)
- `finrl/main.py:51-140` — CLI `train|test|trade`, hardcodes `StockTradingEnv` + `DOW_30_TICKER` + ElegantRL.
- `finrl/train.py`, `test.py`, `trade.py` — thin wrappers around agents + envs.
- `finrl/meta/`:
  - `data_processors/`: `processor_yahoofinance.py`, `processor_alpaca.py:534` (stock), `processor_ccxt.py:146` (Binance fetch-only), + eodhd/joinquant/wrds/quantconnect/sinopac/fxmacrodata.
  - `env_stock_trading/`: `env_stocktrading.py`, `env_stocktrading_np.py` (used by main), `env_stocktrading_cashpenalty.py`, `env_stocktrading_stoploss.py`, `env_stock_papertrading.py`.
  - `env_cryptocurrency_trading/`: `env_multiple_crypto.py:121` (`CryptoEnv`, non-gym), `env_btc_ccxt.py:220` (`BitcoinEnv`, hardcoded scaling).
  - `paper_trading/alpaca.py` (`PaperTradingAlpaca`), `paper_trading/common.py` (stock-centric `DRL_prediction`).
  - `preprocessor/preprocessors.py`, `data_processor.py` (`DataProcessor`).
- `finrl/agents/`: `stablebaselines3/models.py` (`DRLAgent_sb3`), `elegantrl/models.py`, `rllib/`, `fasttd3/`, `portfolio_optimization/`.
- `finrl/applications/`: only `stock_trading/` populated; `cryptocurrency_trading/__init__.py` empty (+ stray `actor.pth`, `recorder.npy`).
- `finrl/config.py:99` — dirs, dates (train 2014–2025, test/trade 2026-01-01–2026-03-20), `INDICATORS` (8), agent params, `ALPACA_API_BASE_URL=https://paper-api.alpaca.markets`, `BINANCE_BASE_URL=https://data.binance.vision/`.
- `finrl/config_private.py:4` — placeholder `xxx` keys (gitignored via `.gitignore:148`).
- `examples/`: stock 2026 pipeline (`_1_data.py`, `_2_train.py`, `_3_Backtest.py`) + ensemble/paper-trading/portfolio notebooks. No crypto example.
- `unit_tests/` (17 files): stock envs, yahoo/alpaca downloaders, SB3/fasttd3, alpaca orders. No crypto tests.
- No `dashboard`, `risk`, `LLM`, `.env`, `frontend/`, `app*.py` files (glob verified 0 hits).

## 3. Dependencies
- `pyproject.toml` (poetry, FinRL 0.3.8) + `requirements.txt:58`: `alpaca-trade-api`, `alpaca-py`, `ccxt`, `yfinance`, `stable-baselines3[extra]`, `sb3-contrib`, `elegantrl` (git), `ray[default,tune]`, `stockstats`, `pyfolio-reloaded`, `wrds`, `jqdatasdk`, `selenium`, `pandas-market-calendars`, etc.
- Docker: `docker/Dockerfile` (`rl-baselines3-zoo` + jupyter). CI: `.github/workflows/test.yml` (cron, build+test).

## 4. Current status
- Works (upstream, not re-verified here): stock data/train/backtest/paper demos.
- Broken/incomplete: crypto envs + ccxt processor + alpaca `fetch_latest_data` NameError risk + `common.py:DRL_prediction` stock-only (see KNOWN_ISSUES).
- Not started: crypto pipeline, risk limits, dashboard, Muse integration, `.env.example`.

## 5. Implementation plan (milestones + acceptance)
- M0 Foundation (done): AGENTS.md + memory + skills. Accept: files exist, no source touched.
- M1 Audit verification: run `pytest unit_tests -q`, `python -m finrl --help`, import check of crypto envs. Accept: results in TEST_HISTORY.
- M2 Crypto data validation: fix/validate `processor_ccxt.py` fetch + indicators on 1 symbol, no look-ahead. Accept: script outputs CSV + test passes.
- M3 Crypto env + backtest: Gymnasium-compatible wrapper, fees/slippage, train/test split. Accept: SB3 PPO trains ≤N steps and backtests with metrics.
- M4 Risk + paper safety: limits, stale-data, kill switch, paper-only guard. Accept: unit tests for each guard.
- M5 Alpaca crypto paper: validate account + symbols, dry-run orders. Accept: paper orders fill or dry-run log, no live URL.
- M6 Muse review (optional, configurable, fallback): timeout + schema validation. Accept: works with and without API key.
- M7 Dashboard (read-only + protected ops): status/portfolio/signals/backtest/orders/risk/logs/stop. Accept: runs locally, ops endpoints auth-protected.
- M8 Release check: full tests + config validation + security scan. Accept: green per verification-and-release skill.
