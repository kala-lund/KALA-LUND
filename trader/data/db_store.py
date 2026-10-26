from __future__ import annotations

"""PostgreSQL (Neon/Supabase) persistent storage for signals, backtests, orders.

Falls back gracefully to an in-memory store ONLY when no DATABASE_URL is
configured (local development) or when ``require_durable`` is false and the
database is unreachable. When ``require_durable`` is true the store NEVER
silently falls back to memory: a write that cannot reach PostgreSQL is
reported as degraded (and raises) instead of being quietly accepted in memory.

Connection guidance (see docs/DEPLOYMENT_NEON.md):
  * Neon: use the **pooled** DSN (host contains ``-pooler``) for the
    application and the unpooled DSN only for migrations/DDL.
  * TLS is enabled automatically for any non-local host when the DSN does not
    already pin ``sslmode``. Neon rejects non-TLS connections.
  * ``connect_timeout`` plus a bounded retry count prevent a dead database from
    blocking API startup forever.

Errors are sanitized through :func:`trader.core.kit.safe_msg`; the connection
URL, host and password are never logged.
"""

import json
import threading
import time
from typing import Any
from urllib.parse import urlparse

from ..core.kit import safe_msg

# Supabase *direct* database endpoints are IPv6-only. On hosts without IPv6
# egress (e.g. Railway) the connection fails even though the credentials are
# correct; the Session Pooler endpoint is required instead.
SUPABASE_DIRECT_SUFFIX = ".supabase.co"
SUPABASE_POOLER_HINT = (
    " Supabase direct connections (db.<ref>.supabase.co:5432) are IPv6-only; "
    "on hosts without IPv6 egress (for example Railway) use the Supabase "
    "Session Pooler connection string (aws-0-<region>.pooler.supabase.com:5432) "
    "instead. No hostname or credential is changed automatically."
)

NEON_HOST_SUFFIX = ".neon.tech"
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "host.docker.internal"}
# Modes that silently disable TLS. Refused for remote hosts; local Postgres is
# allowed to use them for development.
INSECURE_SSLMODES = {"disable", "allow", "prefer"}
_CONNECTION_ERRORS = {"OperationalError", "InterfaceError", "ConnectionError"}


def _is_connection_error(exc: BaseException) -> bool:
    """True for errors that mean the connection is unusable (retry/reconnect).

    Data/constraint/programming errors are deliberately excluded: reconnect
    churn cannot fix them and would mask a real bug as a storage outage.
    """
    return isinstance(exc, ConnectionError) or type(exc).__name__ in _CONNECTION_ERRORS

# Bumping this requires adding the matching migration statements in
# ``_create_tables`` (kept idempotent) and a test.
SCHEMA_VERSION = 1
REQUIRED_TABLES = ("signals", "backtests", "orders", "schema_migrations")
REQUIRED_COLUMNS = {
    "signals": ("id", "symbol", "signal", "confidence", "score", "aggregate", "created_at"),
    "backtests": ("id", "symbol", "timeframe", "metrics", "assumptions", "created_at"),
    "orders": ("id", "symbol", "side", "notional", "status", "client_order_id", "created_at"),
}
REQUIRED_INDEXES = (
    "idx_signals_symbol_created",
    "idx_backtests_symbol_created",
    "idx_orders_client_order_id",
    "idx_orders_open_status",
)


def _host(database_url: str) -> str:
    try:
        return (urlparse(database_url).hostname or "").lower()
    except Exception:  # noqa: BLE001 - malformed URL must never raise here
        return ""


def is_supabase_direct(database_url: str) -> bool:
    """Return True when the DSN points at a Supabase direct (IPv6-only) endpoint.

    Never raises and never inspects credentials.
    """
    host = _host(database_url)
    if not host:
        return False
    # The Session Pooler host ends with ".pooler.supabase.com", which does not
    # match the ".supabase.co" direct suffix, so this is unambiguous.
    return host.endswith(SUPABASE_DIRECT_SUFFIX)


def is_neon(database_url: str) -> bool:
    """Return True for a Neon endpoint. Never raises, never inspects credentials."""
    return _host(database_url).endswith(NEON_HOST_SUFFIX)


def is_local(database_url: str) -> bool:
    return _host(database_url) in LOCAL_HOSTS


def needs_ssl(database_url: str) -> bool:
    """TLS is mandatory for managed remote PostgreSQL (Neon, Supabase)."""
    return bool(database_url) and not is_local(database_url) and "sslmode=" not in database_url.lower()


def connection_hint(database_url: str) -> str:
    """Actionable, credential-free hint for a failed connection."""
    if is_supabase_direct(database_url):
        return SUPABASE_POOLER_HINT
    if is_neon(database_url):
        return (
            " Neon: verify the pooled connection string (host contains '-pooler'), "
            "that the branch/database still exists, and that the password is current. "
            "Neon requires TLS."
        )
    return ""


class DBStore:
    """Persistent storage wrapper.

    Uses PostgreSQL/Supabase when DATABASE_URL is set and psycopg2 is
    available. A dropped connection is re-established lazily (bounded) the next
    time the store is used. When ``require_durable`` is true a failed write is
    never written to memory: it is surfaced as degraded and raised so callers
    cannot mistake it for a durable success.

    The connection URL/password is never logged; errors are sanitized
    through :func:`trader.core.kit.safe_msg`.
    """

    def __init__(self, database_url: str = "", connect_timeout: int = 5,
                 require_durable: bool = False, connect_retries: int = 3,
                 retry_delay: float = 1.0, sslmode: str = "",
                 application_name: str = "finrl-trader") -> None:
        self.url = database_url
        self.connect_timeout = max(1, int(connect_timeout))
        self.require_durable = require_durable
        self.connect_retries = max(1, int(connect_retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.sslmode = (sslmode or "").strip()
        if self.sslmode.lower() in INSECURE_SSLMODES and not is_local(database_url):
            raise RuntimeError(
                "Refusing insecure sslmode for a remote database; use "
                "sslmode=require or verify-full (Neon/Supabase require TLS).")
        self.application_name = application_name or "finrl-trader"
        self._conn: Any = None
        # psycopg2 connections may be shared between API worker threads (FastAPI
        # runs sync endpoints in a thread pool). Serialize cursor use so two
        # requests cannot interleave statements on the same connection.
        self._conn_lock = threading.RLock()
        self._lock = threading.Lock()
        self._mem_signals: list[dict] = []
        self._mem_backtests: list[dict] = []
        self._mem_orders: list[dict] = []
        # Set when a configured database cannot be reached (non-durable mode) or
        # a live connection was lost, so health/dashboard can report the truth.
        self._degraded = False
        self._err: str | None = None
        self._init_db()

    # ---- connection management -------------------------------------------
    def _connect_kwargs(self) -> dict:
        """psycopg2.connect kwargs: bounded latency, TLS for remote hosts."""
        kwargs: dict = {
            "connect_timeout": self.connect_timeout,
            "application_name": self.application_name,
        }
        if self.sslmode:
            kwargs["sslmode"] = self.sslmode
        elif needs_ssl(self.url):
            # Managed PostgreSQL (Neon/Supabase) refuses plaintext connections.
            kwargs["sslmode"] = "require"
        return kwargs

    def _connect_once(self) -> None:
        """Open a fresh connection and (re)create the schema. Raises on failure."""
        try:
            import psycopg2  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "PostgreSQL storage requires psycopg2; install psycopg2-binary."
            ) from exc
        conn = psycopg2.connect(self.url, **self._connect_kwargs())
        conn.autocommit = True
        with self._conn_lock:
            previous, self._conn = self._conn, conn
        if previous is not None and previous is not conn:
            try:
                previous.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        try:
            self._create_tables(conn)
        except Exception:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            with self._conn_lock:
                if self._conn is conn:
                    self._conn = None
            raise

    def _init_db(self) -> None:
        if not self.url:
            if self.require_durable:
                raise RuntimeError(
                    "Durable store required but DATABASE_URL is not configured; "
                    "refusing to fall back to ephemeral in-memory storage.")
            return
        last_exc: Exception | None = None
        for attempt in range(1, self.connect_retries + 1):
            try:
                self._connect_once()
                self._degraded = False
                self._err = None
                return
            except Exception as exc:  # noqa: BLE001 - DB optional unless required
                last_exc = exc
                self._conn = None
                self._err = safe_msg(exc)
                if attempt < self.connect_retries and self.retry_delay:
                    time.sleep(self.retry_delay * attempt)
        self._degraded = True
        if self.require_durable:
            raise RuntimeError(
                "Durable store required but the PostgreSQL connection failed; "
                "refusing to fall back to ephemeral in-memory storage."
                + connection_hint(self.url)
            ) from last_exc

    def _ensure_conn(self) -> bool:
        """Try to (re)establish a live connection. Returns True if one exists.

        Never raises; failures are recorded in ``_err``/``_degraded``.
        """
        if self._conn is not None:
            return True
        if not self.url:
            return False
        # Serialize reconnects: two API threads must not open (and leak) two
        # connections, and _connect_once is reentrant-safe with the RLock.
        with self._conn_lock:
            if self._conn is not None:
                return True
            try:
                self._connect_once()
                self._degraded = False
                self._err = None
                return True
            except Exception as exc:  # noqa: BLE001 - reconnect is best-effort
                self._conn = None
                self._degraded = True
                self._err = safe_msg(exc)
                return False

    def _invalidate_conn(self) -> None:
        """Detach and close the current connection (best effort).

        Used after any query failure: keeping a dead handle would make
        ``_ensure_conn`` return True forever and every later call fail without
        attempting a reconnect.
        """
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _write(self, sql: str, params: tuple) -> bool:
        """Execute a write, reconnecting once if needed.

        Returns True on durable success. When ``require_durable`` is true and the
        write cannot be persisted, raises instead of silently buffering in
        memory. When it is false, records degradation and returns False so the
        caller may use the in-memory fallback.
        """
        for _attempt in (1, 2):
            if not self._ensure_conn():
                break
            try:
                with self._conn_lock:
                    conn = self._conn
                    if conn is None:
                        # Another thread invalidated the handle between
                        # _ensure_conn() and this lock.
                        raise ConnectionError("connection not available")
                    cur = conn.cursor()
                    try:
                        cur.execute(sql, params)
                    finally:
                        cur.close()
                self._degraded = False
                return True
            except Exception as exc:  # noqa: BLE001 - classify before retrying
                self._err = safe_msg(exc)
                if not _is_connection_error(exc):
                    # Data/constraint/programming error: the connection is fine.
                    raise RuntimeError(
                        "database rejected the write (not a connection failure); "
                        f"not retrying. ({self._err})") from exc
                self._degraded = True
                self._invalidate_conn()
        if self.require_durable:
            raise RuntimeError(
                "Durable store write failed and the PostgreSQL connection could "
                "not be re-established; refusing to fall back to ephemeral "
                f"in-memory storage. ({self._err})")
        return False

    # ---- health / lifecycle ----------------------------------------------
    def health_check(self) -> dict:
        """Return a sanitized, independent health report for PostgreSQL.

        Never includes the connection URL, password, or raw error text. A lost
        connection triggers exactly one bounded reconnect attempt before the
        probe, so a recovered database is reported as healthy again.
        """
        if self._conn is None and self.url:
            self._ensure_conn()
        if self._conn is None:
            return {"status": "error", "latency_ms": None,
                    "error": self._err or "no connection"}
        start = time.perf_counter()
        try:
            with self._conn_lock:
                cur = self._conn.cursor()
                try:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                finally:
                    cur.close()
            self._degraded = False
            return {"status": "ok", "latency_ms": round(
                (time.perf_counter() - start) * 1000, 2), "error": None}
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            self._invalidate_conn()
            self._err = safe_msg(exc)
            self._degraded = True
            return {"status": "error", "latency_ms": None,
                    "error": safe_msg(exc)}

    def close(self) -> None:
        """Close the PostgreSQL connection and release resources."""
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _create_tables(self, conn: Any = None) -> None:
        """Create the schema and indexes. Idempotent: safe on every connect."""
        with self._conn_lock:
            target = conn if conn is not None else self._conn
            if target is None:
                return
            cur = target.cursor()
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        id SERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        signal TEXT,
                        confidence REAL,
                        score REAL,
                        aggregate JSONB,
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                    CREATE TABLE IF NOT EXISTS backtests (
                        id SERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        timeframe TEXT,
                        metrics JSONB,
                        assumptions JSONB,
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                    CREATE TABLE IF NOT EXISTS orders (
                        id SERIAL PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        side TEXT,
                        notional REAL,
                        status TEXT,
                        client_order_id TEXT,
                        created_at TIMESTAMP DEFAULT NOW()
                    );
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMP DEFAULT NOW(),
                        note TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_signals_symbol_created
                        ON signals (symbol, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_backtests_symbol_created
                        ON backtests (symbol, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_orders_client_order_id
                        ON orders (client_order_id);
                    CREATE INDEX IF NOT EXISTS idx_orders_open_status
                        ON orders (status) WHERE status IS NOT NULL;
                """)
                cur.execute(
                    "INSERT INTO schema_migrations (version, note) VALUES (%s, %s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (SCHEMA_VERSION, "initial schema"))
            finally:
                cur.close()

    # ---- writes -----------------------------------------------------------
    def save_signal(self, symbol: str, signal_data: dict) -> None:
        ok = self._write(
            "INSERT INTO signals (symbol, signal, confidence, score, aggregate) "
            "VALUES (%s, %s, %s, %s, %s)",
            (symbol, signal_data.get("action"), signal_data.get("confidence"),
             signal_data.get("score"), json.dumps(signal_data)))
        if ok:
            return
        with self._lock:
            self._mem_signals.append({"symbol": symbol, "data": signal_data})

    def save_backtest(self, symbol: str, timeframe: str,
                      metrics: dict, assumptions: dict) -> None:
        ok = self._write(
            "INSERT INTO backtests (symbol, timeframe, metrics, assumptions) "
            "VALUES (%s, %s, %s, %s)",
            (symbol, timeframe, json.dumps(metrics), json.dumps(assumptions)))
        if ok:
            return
        with self._lock:
            self._mem_backtests.append({
                "symbol": symbol, "timeframe": timeframe,
                "metrics": metrics, "assumptions": assumptions})

    def save_order(self, symbol: str, side: str, notional: float,
                   status: str, client_order_id: str) -> None:
        ok = self._write(
            "INSERT INTO orders (symbol, side, notional, status, client_order_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (symbol, side, notional, status, client_order_id))
        if ok:
            return
        with self._lock:
            self._mem_orders.append({
                "symbol": symbol, "side": side, "notional": notional,
                "status": status, "client_order_id": client_order_id})

    # ---- reads ------------------------------------------------------------
    def stats(self) -> dict:
        """Cheap, query-free status. ``signals``/``backtests``/``orders`` are
        record counts buffered **in memory only** (they are not database row
        counts; see :meth:`row_counts`)."""
        with self._lock:
            return {
                "backend": "postgres" if self._conn is not None else "memory",
                "configured": bool(self.url),
                "durable": self._conn is not None and not self._degraded,
                "degraded": bool(self._degraded),
                "error": self._err,
                "signals": len(self._mem_signals),
                "backtests": len(self._mem_backtests),
                "orders": len(self._mem_orders),
            }

    def row_counts(self) -> dict[str, int]:
        """Return exact row counts for the durable tables.

        Raises ``RuntimeError`` when no durable connection is available so a
        caller can never mistake ``{}`` for "zero rows".
        """
        if not self._ensure_conn():
            raise RuntimeError(self._err or "database unavailable")
        try:
            with self._conn_lock:
                cur = self._conn.cursor()
                try:
                    cur.execute(
                        "SELECT (SELECT count(*) FROM signals), "
                        "(SELECT count(*) FROM backtests), "
                        "(SELECT count(*) FROM orders), "
                        "(SELECT count(*) FROM schema_migrations)")
                    row = cur.fetchone()
                finally:
                    cur.close()
            self._degraded = False
            return {"signals": int(row[0]), "backtests": int(row[1]),
                    "orders": int(row[2]), "schema_migrations": int(row[3])}
        except Exception as exc:  # noqa: BLE001 - surface as an explicit failure
            self._err = safe_msg(exc)
            self._degraded = True
            self._invalidate_conn()
            raise RuntimeError(f"row count query failed: {self._err}") from exc

    def verify(self) -> dict:
        """Verify schema, indexes, migration version and row counts.

        Returns a sanitized report (never credentials). ``ok`` is false when
        anything is missing or the schema version is not current.
        """
        report: dict = {"ok": False, "schema_version": None,
                        "expected_version": SCHEMA_VERSION, "tables": {},
                        "missing_tables": [], "missing_columns": {},
                        "missing_indexes": [], "counts": {}, "error": None}
        if not self._ensure_conn():
            report["error"] = self._err or "database unavailable"
            return report
        try:
            with self._conn_lock:
                cur = self._conn.cursor()
                try:
                    cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'")
                    tables = {r[0] for r in cur.fetchall()}
                    report["tables"] = {t: t in tables for t in REQUIRED_TABLES}
                    report["missing_tables"] = [t for t in REQUIRED_TABLES if t not in tables]

                    for table, expected in REQUIRED_COLUMNS.items():
                        if table not in tables:
                            report["missing_columns"][table] = list(expected)
                            continue
                        cur.execute(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = %s",
                            (table,))
                        present = {r[0] for r in cur.fetchall()}
                        missing = [c for c in expected if c not in present]
                        if missing:
                            report["missing_columns"][table] = missing

                    cur.execute(
                        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
                    indexes = {r[0] for r in cur.fetchall()}
                    report["missing_indexes"] = [
                        i for i in REQUIRED_INDEXES if i not in indexes]

                    cur.execute(
                        "SELECT max(version) FROM schema_migrations")
                    version = cur.fetchone()[0]
                    report["schema_version"] = int(version) if version is not None else None

                    cur.execute(
                        "SELECT (SELECT count(*) FROM signals), "
                        "(SELECT count(*) FROM backtests), "
                        "(SELECT count(*) FROM orders), "
                        "(SELECT count(*) FROM schema_migrations)")
                    row = cur.fetchone()
                    report["counts"] = {
                        "signals": int(row[0]), "backtests": int(row[1]),
                        "orders": int(row[2]), "schema_migrations": int(row[3])}
                finally:
                    cur.close()
            report["ok"] = (
                not report["missing_tables"]
                and not report["missing_columns"]
                and not report["missing_indexes"]
                and report["schema_version"] == SCHEMA_VERSION)
            self._degraded = False
            return report
        except Exception as exc:  # noqa: BLE001 - report, never crash the caller
            self._err = safe_msg(exc)
            self._degraded = True
            self._invalidate_conn()
            report["error"] = self._err
            return report

    def get_open_orders(self):
        """Return (open_orders, error). Reads outstanding orders from orders table.

        Never mutates data. Falls back to in-memory orders when no database is
        configured, or when a configured database is unreachable in
        non-durable mode. On query failure returns ([], error_string).
        """
        open_statuses = {
            "new", "accepted", "pending_new", "partially_filled", "held",
            "accepted_for_bidding", "stopped", "replaced",
            "pending_replace", "pending_cancel", "calculated",
        }
        if self._conn is None and self.url and not self._ensure_conn():
            # Configured but unreachable: report, never claim durable success.
            if self.require_durable:
                return [], (self._err or "durable store unavailable")
            with self._lock:
                out = [dict(o) for o in self._mem_orders
                       if o.get("status") in open_statuses]
            return out, (self._err or "database unavailable; using in-memory orders")
        if self._conn is None:
            with self._lock:
                out = [dict(o) for o in self._mem_orders
                       if o.get("status") in open_statuses]
            return out, None
        try:
            with self._conn_lock:
                cur = self._conn.cursor()
                try:
                    cur.execute(
                        "SELECT symbol, side, notional, status, client_order_id "
                        "FROM orders WHERE status NOT IN (%s, %s, %s, %s, %s)",
                        ("filled", "canceled", "rejected", "expired", "done_for_day"))
                    rows = cur.fetchall()
                finally:
                    cur.close()
            out = [
                {"symbol": r[0], "side": r[1],
                 "notional": float(r[2] or 0),
                 "status": r[3], "client_order_id": r[4]}
                for r in rows
            ]
            return out, None
        except Exception:  # noqa: BLE001
            self._invalidate_conn()
            return [], "db open-orders query failed"
