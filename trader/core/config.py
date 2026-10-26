from __future__ import annotations

"""Central configuration. Secrets only from environment / repo .env (never committed)."""

import os
from dataclasses import dataclass, field

from ..execution.safety import PAPER_TRADING_ENDPOINT

FULL_USD_UNIVERSE: tuple = (
    "AAVE/USD", "ADA/USD", "ARB/USD", "AVAX/USD", "BAT/USD", "BCH/USD",
    "BONK/USD", "BTC/USD", "CRV/USD", "DOGE/USD", "DOT/USD", "ETH/USD",
    "FIL/USD", "GRT/USD", "HYPE/USD", "LDO/USD", "LINK/USD", "LTC/USD",
    "ONDO/USD", "PAXG/USD", "PEPE/USD", "POL/USD", "RENDER/USD", "SHIB/USD",
    "SKY/USD", "SOL/USD", "SUSHI/USD", "TRUMP/USD", "UNI/USD", "WIF/USD",
    "XRP/USD", "XTZ/USD", "YFI/USD",
)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for cand in (os.path.join(root, ".env"), os.path.join(os.getcwd(), ".env")):
        if os.path.isfile(cand):
            load_dotenv(cand, override=False)
            return


_load_dotenv()


def _s(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n, "")
        if v:
            return v
    return default


def _b(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y", "on")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(*names: str, default: int) -> int:
    for n in names:
        if os.environ.get(n):
            try:
                return int(float(os.environ[n]))
            except (TypeError, ValueError):
                continue
    return default


@dataclass(frozen=True)
class Settings:
    trading_mode: str = "paper"
    enable_paper_orders: bool = False
    enable_live_trading: bool = False
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    crypto_symbols: tuple = ("BTC/USD", "ETH/USD", "SOL/USD")
    primary_timeframe: str = "15Min"
    data_provider: str = "alpaca"
    cache_ttl: int = 300
    cache_max: int = 1000
    fetch_workers: int = 8
    fetch_bars: int = 150
    claude_enabled: bool = False
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-4-5"
    claude_base_url: str = "https://api.anthropic.com"
    claude_timeout: int = 15
    claude_max_tokens: int = 1024
    max_order_notional: float = 500.0
    max_position_notional: float = 2000.0
    max_exposure: float = 5000.0
    max_concentration: float = 0.5
    max_daily_loss: float = 300.0
    max_drawdown: float = 0.15
    max_open_orders: int = 10
    min_buying_power: float = 100.0
    stale_seconds: int = 900
    fee_pct: float = 0.0025
    slippage_pct: float = 0.0005
    take_profit_pct: float = 0.0
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # CORS origins for dashboard / API access (never use wildcard for authenticated endpoints)
    cors_origins: list[str] = field(default_factory=lambda: ["http://127.0.0.1:8000", "http://localhost:8000"])
    # Honour X-Forwarded-For only behind a trusted proxy (Railway edge).
    trust_proxy_headers: bool = False
    dashboard_password: str = ""
    automate_enabled: bool = False
    automate_minutes: int = 15
    automate_min_conf: float = 0.5
    automate_max_notional: float = 100.0
    automate_dry_run: bool = True
    # Redis (fast caching for market data, signals)
    redis_url: str = ""
    redis_ttl: int = 300
    # Supabase / PostgreSQL (persistent storage for signals, backtests, orders)
    database_url: str = ""
    # Bounded database connect behaviour: a dead/unreachable database must not
    # block API startup indefinitely.
    database_connect_timeout: int = 5
    database_connect_retries: int = 3
    # Empty = auto (TLS required for any non-local host, e.g. Neon/Supabase).
    database_sslmode: str = ""
    database_application_name: str = "finrl-trader"
    # When true the service refuses to start without a working durable store.
    # Use in production (Railway) so trading state is never kept ephemerally.
    require_durable_store: bool = False
    # Hard upper bound (seconds) for READ-ONLY broker calls. alpaca-py never
    # passes a timeout to requests.Session, so without this a hung connection
    # can block an API request thread indefinitely.
    broker_read_timeout: float = 10.0
    weights: dict = field(default_factory=lambda: {
        "trend": 1.0, "momentum": 1.0, "mean_reversion": 1.0,
        "volatility": 0.5, "volume_regime": 0.5, "stat_relationship": 0.5})


def load_settings() -> Settings:
    raw = _s("CRYPTO_SYMBOLS", default="BTC/USD,ETH/USD,SOL/USD").strip()
    syms = FULL_USD_UNIVERSE if raw.upper() == "ALL" else tuple(x.strip() for x in raw.split(",") if x.strip())
    url = _s("ALPACA_PAPER_BASE_URL", "APCA_PAPER1_BASE_URL", "APCA_BASE_URL",
             default="https://paper-api.alpaca.markets").rstrip("/")
    if url.lower().endswith("/v2"):
        url = url[:-3].rstrip("/")
    return Settings(
        trading_mode=_s("TRADING_MODE", default="paper") or "paper",
        enable_paper_orders=_b("ENABLE_PAPER_ORDERS", False),
        enable_live_trading=_b("ENABLE_LIVE_TRADING", False),
        alpaca_api_key=_s("ALPACA_API_KEY", "APCA_API_KEY_ID", "APCA_API_KEY", "APCA_PAPER1_API_KEY"),
        alpaca_api_secret=_s("ALPACA_API_SECRET", "APCA_API_SECRET_KEY", "APCA_API_SECRET", "APCA_PAPER1_API_SECRET"),
        alpaca_paper_base_url=url,
        crypto_symbols=syms or ("BTC/USD", "ETH/USD", "SOL/USD"),
        primary_timeframe=_s("PRIMARY_TIMEFRAME", default="15Min"),
        data_provider=_s("DATA_PROVIDER", default="alpaca"),
        cache_ttl=_i("DATA_CACHE_TTL_SECONDS", default=300),
        cache_max=_i("DATA_CACHE_MAX_ENTRIES", default=1000),
        fetch_workers=_i("FETCH_WORKERS", default=8),
        fetch_bars=_i("FETCH_BARS", default=150),
        claude_enabled=_b("CLAUDE_ENABLED", False),
        claude_api_key=_s("CLAUDE_API_KEY", "META_API_KEY", "ANTHROPIC_API_KEY"),
        claude_model=_s("CLAUDE_MODEL", "META_MODEL", default="claude-sonnet-4-5"),
        claude_base_url=_s("CLAUDE_BASE_URL", default="https://api.anthropic.com"),
        claude_timeout=_i("CLAUDE_TIMEOUT_SECONDS", default=15),
        claude_max_tokens=_i("CLAUDE_MAX_TOKENS", default=1024),
        max_order_notional=_f("MAX_ORDER_NOTIONAL", 500.0),
        max_position_notional=_f("MAX_POSITION_NOTIONAL", 2000.0),
        max_exposure=_f("MAX_PORTFOLIO_EXPOSURE", 5000.0),
        max_concentration=_f("MAX_CONCENTRATION_PCT", 0.5),
        max_daily_loss=_f("MAX_DAILY_LOSS", 300.0),
        max_drawdown=_f("MAX_DRAWDOWN_PCT", 0.15),
        max_open_orders=_i("MAX_OPEN_ORDERS", default=10),
        min_buying_power=_f("MIN_BUYING_POWER", 100.0),
        stale_seconds=_i("STALE_DATA_SECONDS", default=900),
        fee_pct=_f("BACKTEST_FEE_PCT", 0.0025),
        slippage_pct=_f("BACKTEST_SLIPPAGE_PCT", 0.0005),
        take_profit_pct=_f("TAKE_PROFIT_PCT", 0.0),
        api_host=_s("API_HOST", "WEB_HOST", default="127.0.0.1"),
        api_port=_i("API_PORT", "WEB_PORT", "PORT", default=8000),
        cors_origins=[o.strip() for o in _s("CORS_ORIGINS", default="http://127.0.0.1:8000,http://localhost:8000").split(",") if o.strip()],
        # Only trust X-Forwarded-For when the app is behind a proxy that appends
        # the real client address (Railway). Default false: a direct client can
        # otherwise rotate the header to bypass password rate limiting.
        trust_proxy_headers=_b("TRUST_PROXY_HEADERS", False),
        dashboard_password=_s("DASHBOARD_PASSWORD", default=""),
        automate_enabled=_b("AUTOMATE_ENABLED", False),
        automate_minutes=_i("AUTOMATE_INTERVAL_MINUTES", default=15),
        automate_min_conf=_f("AUTOMATE_MIN_CONFIDENCE", 0.5),
        automate_max_notional=_f("AUTOMATE_MAX_NOTIONAL", 100.0),
        automate_dry_run=_b("AUTOMATE_DRY_RUN", True),
        redis_url=_s("REDIS_URL", default=""),
        redis_ttl=_i("REDIS_TTL_SECONDS", default=300),
        database_url=_s("DATABASE_URL", default=""),
        database_connect_timeout=_i("DB_CONNECT_TIMEOUT", "POSTGRES_CONNECT_TIMEOUT", default=5),
        database_connect_retries=_i("DB_CONNECT_RETRIES", default=3),
        database_sslmode=_s("DB_SSLMODE", default=""),
        database_application_name=_s("DB_APPLICATION_NAME", default="finrl-trader"),
        require_durable_store=_b("REQUIRE_DURABLE_STORE", False),
        broker_read_timeout=_f("BROKER_READ_TIMEOUT_SECONDS", 10.0),
    )


def validate_settings(s: Settings) -> list[str]:
    e: list[str] = []
    if s.enable_live_trading:
        e.append("ENABLE_LIVE_TRADING must be false: live trading is disabled.")
    if s.trading_mode != "paper":
        e.append(f"TRADING_MODE must be 'paper', got {s.trading_mode!r}.")
    if s.alpaca_paper_base_url != PAPER_TRADING_ENDPOINT:
        e.append("Paper URL must equal the approved Alpaca paper endpoint.")
    if s.enable_paper_orders and not (s.alpaca_api_key and s.alpaca_api_secret):
        e.append("Paper orders on but credentials missing.")
    if not s.crypto_symbols:
        e.append("No symbols configured.")
    if s.automate_minutes < 1:
        e.append("AUTOMATE_INTERVAL_MINUTES must be >= 1.")
    if "*" in s.cors_origins:
        e.append("CORS_ORIGINS must not contain '*': list exact frontend origins.")
    if s.require_durable_store and not s.database_url:
        e.append("REQUIRE_DURABLE_STORE is set but DATABASE_URL is missing; "
                 "refusing to run with ephemeral in-memory storage.")
    if s.database_url:
        scheme = s.database_url.split(":", 1)[0].strip().lower()
        if scheme not in ("postgresql", "postgres"):
            e.append("DATABASE_URL must be a postgresql:// or postgres:// URI.")
        if s.database_connect_timeout < 1:
            e.append("DB_CONNECT_TIMEOUT must be >= 1 second.")
        if s.database_connect_retries < 1:
            e.append("DB_CONNECT_RETRIES must be >= 1.")
    return e
