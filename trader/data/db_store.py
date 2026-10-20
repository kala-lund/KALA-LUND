from __future__ import annotations

"""PostgreSQL/Supabase persistent storage for signals, backtests, orders.

Falls back gracefully if DATABASE_URL is not configured or psycopg2 is not
installed, so the system keeps working without a database.
"""

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from ..core.kit import safe_msg


class DBStore:
    """Persistent storage wrapper.

    Uses PostgreSQL/Supabase when DATABASE_URL is set and psycopg2 is
    available. Otherwise falls back to an in-memory store so the system
    keeps working without a database.

    The connection URL/password is never logged; errors are sanitized
    through :func:`trader.core.kit.safe_msg`.
    """

    def __init__(self, database_url: str = "", connect_timeout: int = 5,
                 require_durable: bool = False) -> None:
        self.url = database_url
        self.connect_timeout = connect_timeout
        self.require_durable = require_durable
        self._conn: Any = None
        self._lock = threading.Lock()
        self._mem_signals: list[dict] = []
        self._mem_backtests: list[dict] = []
        self._mem_orders: list[dict] = []
        self._init_db()

    def _init_db(self) -> None:
        if not self.url:
            if self.require_durable:
                raise RuntimeError(
                    "Durable store required but DATABASE_URL is not configured; "
                    "refusing to fall back to ephemeral in-memory storage.")
            return
        try:
            import psycopg2  # type: ignore
            self._conn = psycopg2.connect(
                self.url, connect_timeout=self.connect_timeout)
            self._conn.autocommit = True
            self._create_tables()
        except Exception as exc:  # noqa: BLE001 - DB optional, fall back to memory
            self._conn = None
            if self.require_durable:
                raise RuntimeError(
                    "Durable store required but the PostgreSQL connection failed; "
                    "refusing to fall back to ephemeral in-memory storage."
                ) from exc

    def health_check(self) -> dict:
        """Return a sanitized, independent health report for PostgreSQL.

        Never includes the connection URL, password, or raw error text.
        """
        if self._conn is None:
            return {"status": "error", "latency_ms": None,
                    "error": "no connection"}
        import time
        start = time.perf_counter()
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            return {"status": "ok", "latency_ms": round(
                (time.perf_counter() - start) * 1000, 2), "error": None}
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            try:
                self._conn.rollback()
            except Exception:
                pass
            return {"status": "error", "latency_ms": None,
                    "error": safe_msg(exc)}

    def close(self) -> None:
        """Close the PostgreSQL connection and release resources."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
                self._conn = None

    def _create_tables(self) -> None:
        if self._conn is None:
            return
        cur = self._conn.cursor()
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
        """)
        cur.close()

    def save_signal(self, symbol: str, signal_data: dict) -> None:
        if self._conn is not None:
            try:
                cur = self._conn.cursor()
                cur.execute(
                    "INSERT INTO signals (symbol, signal, confidence, score, aggregate) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (symbol, signal_data.get("action"), signal_data.get("confidence"),
                     signal_data.get("score"), json.dumps(signal_data)))
                cur.close()
                return
            except Exception:  # noqa: BLE001
                try:
                    self._conn.rollback()
                except Exception:
                    pass
        with self._lock:
            self._mem_signals.append({"symbol": symbol, "data": signal_data})

    def save_backtest(self, symbol: str, timeframe: str,
                      metrics: dict, assumptions: dict) -> None:
        if self._conn is not None:
            try:
                cur = self._conn.cursor()
                cur.execute(
                    "INSERT INTO backtests (symbol, timeframe, metrics, assumptions) "
                    "VALUES (%s, %s, %s, %s)",
                    (symbol, timeframe, json.dumps(metrics), json.dumps(assumptions)))
                cur.close()
                return
            except Exception:  # noqa: BLE001
                try:
                    self._conn.rollback()
                except Exception:
                    pass
        with self._lock:
            self._mem_backtests.append({
                "symbol": symbol, "timeframe": timeframe,
                "metrics": metrics, "assumptions": assumptions})

    def save_order(self, symbol: str, side: str, notional: float,
                   status: str, client_order_id: str) -> None:
        if self._conn is not None:
            try:
                cur = self._conn.cursor()
                cur.execute(
                    "INSERT INTO orders (symbol, side, notional, status, client_order_id) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (symbol, side, notional, status, client_order_id))
                cur.close()
                return
            except Exception:  # noqa: BLE001
                try:
                    self._conn.rollback()
                except Exception:
                    pass
        with self._lock:
            self._mem_orders.append({
                "symbol": symbol, "side": side, "notional": notional,
                "status": status, "client_order_id": client_order_id})

    def stats(self) -> dict:
        with self._lock:
            return {
                "backend": "postgres" if self._conn is not None else "memory",
                "signals": len(self._mem_signals),
                "backtests": len(self._mem_backtests),
                "orders": len(self._mem_orders),
            }

    def get_open_orders(self):
        """Return (open_orders, error). Reads outstanding orders from orders table.

        Never mutates data. Falls back to in-memory orders when no DB
        connection. On query failure returns ([], error_string).
        """
        open_statuses = {
            "new", "accepted", "pending_new", "partially_filled", "held",
            "accepted_for_bidding", "stopped", "replaced",
            "pending_replace", "pending_cancel", "calculated",
        }
        if self._conn is None:
            with self._lock:
                out = [dict(o) for o in self._mem_orders
                       if o.get("status") in open_statuses]
            return out, None
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT symbol, side, notional, status, client_order_id "
                "FROM orders WHERE status NOT IN (%s, %s, %s, %s, %s)",
                ("filled", "canceled", "rejected", "expired", "done_for_day"))
            rows = cur.fetchall()
            cur.close()
            out = [
                {"symbol": r[0], "side": r[1],
                 "notional": float(r[2] or 0),
                 "status": r[3], "client_order_id": r[4]}
                for r in rows
            ]
            return out, None
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            return [], "db open-orders query failed"
