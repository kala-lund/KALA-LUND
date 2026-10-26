"""Hermetic test defaults for the Trader V2 suite.

The suite must never reach a broker, a live market, or the developer's local
``.env`` / database / cache. Every value below is applied **before**
``trader.core.config`` is imported, and ``load_dotenv(override=False)`` never
overrides variables that are already present in ``os.environ``.

This keeps CI and local runs identical and credential-free.
"""

from __future__ import annotations

import os

# Key: environment variable, value: safe default for tests.
_SAFE_TEST_ENV: dict[str, str] = {
    # Paper-only safety defaults.
    "TRADING_MODE": "paper",
    "ENABLE_LIVE_TRADING": "false",
    "ENABLE_PAPER_ORDERS": "false",
    # Never start the automation loop during tests: it performs broker calls.
    "AUTOMATE_ENABLED": "false",
    # Never require a durable store during tests (no database in CI).
    "REQUIRE_DURABLE_STORE": "false",
    # Do not connect to the developer's PostgreSQL/Redis instances.
    "DATABASE_URL": "",
    "REDIS_URL": "",
    # Never reach Alpaca (or any market data API) from a test run. Every alias
    # accepted by trader.core.config must be blanked: _s() skips empty values,
    # so leaving an APCA_* alias set in the developer's .env would leak through.
    "ALPACA_API_KEY": "",
    "ALPACA_API_SECRET": "",
    "APCA_API_KEY_ID": "",
    "APCA_API_KEY": "",
    "APCA_PAPER1_API_KEY": "",
    "APCA_API_SECRET_KEY": "",
    "APCA_API_SECRET": "",
    "APCA_PAPER1_API_SECRET": "",
    # Force the provider that fails closed without credentials.
    "DATA_PROVIDER": "alpaca",
    # Operations stay locked (fail-closed) unless a test sets a password.
    "DASHBOARD_PASSWORD": "",
    # Optional Muse AI must stay disabled (no outbound LLM calls in tests).
    "CLAUDE_ENABLED": "false",
    "CLAUDE_API_KEY": "",
}

for _key, _value in _SAFE_TEST_ENV.items():
    os.environ[_key] = _value
