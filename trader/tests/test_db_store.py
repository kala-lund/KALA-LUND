from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock

import pytest

from trader.data.db_store import DBStore


class OperationalError(Exception):
    """Named exactly like psycopg2's connection error (classified by name)."""


def _make_conn_mock(execute_side=None):
    conn = MagicMock()
    conn.autocommit = True
    cur = MagicMock()
    conn.cursor.return_value = cur
    if execute_side is not None:
        cur.execute.side_effect = execute_side
    else:
        cur.fetchone.return_value = (1,)
    return conn


def test_db_store_memory_fallback_no_url():
    """No DATABASE_URL -> in-memory fallback, health reports error."""
    db = DBStore(database_url="")
    assert db._conn is None
    hc = db.health_check()
    assert hc["status"] == "error"
    assert hc["latency_ms"] is None
    assert hc["error"] == "no connection"
    db.save_signal("BTC/USD", {"action": "BUY", "confidence": 0.8, "score": 0.5})
    stats = db.stats()
    assert stats["backend"] == "memory"
    assert stats["signals"] == 1


@patch("psycopg2.connect")
def test_db_store_connect_timeout_passed(mock_connect):
    """connect_timeout is forwarded to psycopg2.connect."""
    mock_connect.return_value = _make_conn_mock()
    db = DBStore(database_url="postgresql://user:pass@host:5432/db",
                 connect_timeout=3)
    _, kwargs = mock_connect.call_args
    assert kwargs["connect_timeout"] == 3
    db.close()


@patch("psycopg2.connect")
def test_db_store_health_check_ok(mock_connect):
    """SELECT 1 succeeds -> health ok, no URL/password leaked."""
    mock_connect.return_value = _make_conn_mock()
    db = DBStore(database_url="postgresql://user:secretpass@host:5432/db")
    hc = db.health_check()
    assert hc["status"] == "ok"
    assert hc["latency_ms"] is not None
    assert hc["error"] is None
    assert "secretpass" not in str(hc)
    db.close()


@patch("psycopg2.connect")
def test_db_store_health_check_fail(mock_connect):
    """SELECT 1 fails -> health error, password sanitized."""

    def execute_side(sql, *args):
        if "SELECT 1" in str(sql):
            raise Exception("connection lost: user:secretpass@host")
        return None

    mock_connect.return_value = _make_conn_mock(execute_side=execute_side)
    db = DBStore(database_url="postgresql://user:secretpass@host:5432/db")
    hc = db.health_check()
    assert hc["status"] == "error"
    assert hc["error"] is not None
    assert "secretpass" not in hc["error"]
    assert "user:secretpass" not in hc["error"]
    db.close()


@patch("psycopg2.connect")
def test_db_store_close_and_health_reconnects(mock_connect):
    """close() releases the connection; health_check re-establishes it once."""
    conn = _make_conn_mock()
    mock_connect.return_value = conn
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    db.close()
    conn.close.assert_called_once()
    assert db._conn is None
    hc = db.health_check()
    # A dropped connection must recover instead of reporting a permanent error.
    assert hc["status"] == "ok"
    assert mock_connect.call_count == 2
    db.close()


@patch("psycopg2.connect")
def test_db_store_ssl_required_for_remote_and_not_local(mock_connect):
    """TLS is enforced for managed remote hosts; local Postgres is unchanged."""
    mock_connect.return_value = _make_conn_mock()
    remote = DBStore(database_url="postgresql://u:p@db.example.com:5432/x")
    assert remote._connect_kwargs()["sslmode"] == "require"
    assert remote._connect_kwargs()["connect_timeout"] == 5
    assert remote._connect_kwargs()["application_name"] == "finrl-trader"

    local = DBStore(database_url="postgresql://postgres:postgres@localhost:5432/finrl")
    assert "sslmode" not in local._connect_kwargs()

    explicit = DBStore(database_url="postgresql://u:p@db.example.com/x",
                       sslmode="verify-full", connect_timeout=7)
    assert explicit._connect_kwargs()["sslmode"] == "verify-full"
    assert explicit._connect_kwargs()["connect_timeout"] == 7
    remote.close()
    local.close()
    explicit.close()


def test_db_store_ssl_detection_never_leaks_or_raises():
    from trader.data.db_store import connection_hint, is_local, is_neon, needs_ssl

    assert is_neon("postgresql://u:p@ep-x-pooler.us-east-2.aws.neon.tech/db?sslmode=require")
    assert not is_neon("postgresql://u:p@localhost/db")
    assert is_local("postgresql://u:p@127.0.0.1/db")
    assert needs_ssl("postgresql://u:p@ep-x.neon.tech/db")
    assert not needs_ssl("postgresql://u:p@ep-x.neon.tech/db?sslmode=require")
    hint = connection_hint("postgresql://u:secret@ep-x.neon.tech/db")
    assert "Neon" in hint and "secret" not in hint


@patch("psycopg2.connect")
def test_db_store_schema_has_migrations_and_indexes(mock_connect):
    """Schema creation is idempotent and includes migrations + read indexes."""
    conn = _make_conn_mock()
    mock_connect.return_value = conn
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    cur = conn.cursor.return_value
    executed = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
    for marker in ("schema_migrations", "idx_signals_symbol_created",
                   "idx_backtests_symbol_created", "idx_orders_client_order_id",
                   "idx_orders_open_status", "ON CONFLICT (version) DO NOTHING"):
        assert marker in executed
    db.close()


@patch("psycopg2.connect")
def test_db_store_row_counts_exact_values(mock_connect):
    conn = _make_conn_mock()
    cur = conn.cursor.return_value
    cur.fetchone.return_value = (3, 2, 1, 1)
    mock_connect.return_value = conn
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    assert db.row_counts() == {"signals": 3, "backtests": 2,
                               "orders": 1, "schema_migrations": 1}
    db.close()


def test_db_store_row_counts_raises_without_database():
    db = DBStore(database_url="")
    with pytest.raises(RuntimeError):
        db.row_counts()
    report = db.verify()
    assert report["ok"] is False
    assert report["error"] == "database unavailable"


@patch("psycopg2.connect")
def test_db_store_verify_failure_reconnects_next_time(mock_connect):
    """A failed verification drops the dead handle so the next call reconnects."""

    def execute_side(sql, *args):
        if "information_schema.tables" in str(sql):
            raise Exception("connection lost while verifying")
        return None

    mock_connect.return_value = _make_conn_mock(execute_side=execute_side)
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    assert db.verify()["ok"] is False
    assert db._conn is None
    db.verify()
    assert mock_connect.call_count >= 2
    db.close()


@patch("psycopg2.connect")
def test_db_store_rejects_insecure_ssl_for_remote(mock_connect):
    with pytest.raises(RuntimeError, match="insecure sslmode"):
        DBStore(database_url="postgresql://u:p@db.example.com/x", sslmode="disable")
    mock_connect.assert_not_called()

    # Local development Postgres may still use it explicitly.
    mock_connect.return_value = _make_conn_mock()
    local = DBStore(database_url="postgresql://postgres:postgres@localhost/db",
                    sslmode="disable")
    assert local._connect_kwargs()["sslmode"] == "disable"
    local.close()


@patch("psycopg2.connect")
def test_db_store_durable_write_raises_after_connection_loss(mock_connect):
    class OperationalError(Exception):
        pass

    def execute_side(sql, *args):
        if "INSERT INTO orders" in str(sql):
            raise OperationalError("server closed the connection")
        return None

    mock_connect.return_value = _make_conn_mock(execute_side=execute_side)
    db = DBStore(database_url="postgresql://user:pass@host:5432/db",
                 require_durable=True)
    with pytest.raises(RuntimeError, match="refusing to fall back"):
        db.save_order("BTC/USD", "BUY", 100.0, "filled", "abc123")
    assert mock_connect.call_count >= 2  # reconnect attempted once
    assert not db._mem_orders  # never silently buffered
    db.close()


@patch("psycopg2.connect")
def test_db_store_data_error_does_not_tear_down_connection(mock_connect):
    def execute_side(sql, *args):
        if "INSERT INTO orders" in str(sql):
            raise ValueError("oversized value")
        return None

    mock_connect.return_value = _make_conn_mock(execute_side=execute_side)
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    with pytest.raises(RuntimeError, match="not retrying"):
        db.save_order("BTC/USD", "BUY", 100.0, "filled", "abc123")
    assert db._conn is not None
    assert mock_connect.call_count == 1
    db.close()


@patch("psycopg2.connect")
def test_db_store_save_order_postgres_path(mock_connect):
    """Orders write through the connection when it is open."""
    conn = _make_conn_mock()
    mock_connect.return_value = conn
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    db.save_order("BTC/USD", "BUY", 100.0, "filled", "abc123")
    cur = conn.cursor.return_value
    cur.execute.assert_called()
    db.close()


@patch("psycopg2.connect")
def test_stats_names_configured_backend_even_when_disconnected(mock_connect):
    """A dropped handle must never be reported as an in-memory store.

    Regression: diagnostics returned ``backend=memory, durable=false`` while the
    health probe (which reconnects) reported postgres.
    """
    mock_connect.return_value = _make_conn_mock()
    db = DBStore(database_url="postgresql://user:pass@host:5432/db",
                 require_durable=True)
    db._invalidate_conn()

    stats = db.stats()
    assert stats["backend"] == "postgres"
    assert stats["configured"] is True
    assert stats["connected"] is False
    assert stats["durable"] is False
    assert stats["degraded"] is True
    # require_durable never buffers writes in memory, so no fallback is active.
    assert stats["fallback_memory"] is False
    db.close()


@patch("psycopg2.connect")
def test_durable_available_probes_and_recovers(mock_connect):
    """The trading gate actively reconnects instead of trusting a dead handle."""
    mock_connect.return_value = _make_conn_mock()
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    db._invalidate_conn()
    verdict = db.durable_available()
    assert verdict["ok"] is True
    assert verdict["backend"] == "postgres"
    assert "secretpass" not in str(verdict)
    db.close()


@patch("psycopg2.connect")
def test_durable_available_reports_failure_without_leaking_dsn(mock_connect):
    def execute_side(sql, *args):
        if "SELECT 1" in str(sql):
            raise Exception("could not connect to postgresql://user:secretpass@host/db")
        return None

    mock_connect.return_value = _make_conn_mock(execute_side=execute_side)
    db = DBStore(database_url="postgresql://user:secretpass@host:5432/db")
    verdict = db.durable_available()
    assert verdict["ok"] is False
    assert verdict["backend"] == "postgres"
    assert "secretpass" not in str(verdict)
    db.close()


def test_durable_available_is_false_when_required_but_unconfigured():
    db = DBStore(database_url="")
    db.require_durable = True
    verdict = db.durable_available()
    assert verdict["ok"] is False
    assert verdict["backend"] == "none"


@patch("psycopg2.connect")
def test_get_open_orders_failure_marks_store_degraded(mock_connect):
    """A connection-class read failure must not leave a 'healthy' status."""
    def execute_side(sql, *args):
        if "FROM orders" in str(sql):
            raise OperationalError("server closed the connection unexpectedly")
        return None

    mock_connect.return_value = _make_conn_mock(execute_side=execute_side)
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    orders, err = db.get_open_orders()
    assert orders == []
    assert err == "db open-orders query failed"
    stats = db.stats()
    assert stats["degraded"] is True
    assert stats["durable"] is False
    assert stats["backend"] == "postgres"
    db.close()


@patch("psycopg2.connect")
def test_get_open_orders_data_error_keeps_connection(mock_connect):
    """A data/constraint error is not a storage outage: keep the connection."""
    def execute_side(sql, *args):
        if "FROM orders" in str(sql):
            raise ValueError("operator does not exist: text = integer")
        return None

    mock_connect.return_value = _make_conn_mock(execute_side=execute_side)
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    orders, err = db.get_open_orders()
    assert orders == [] and err
    stats = db.stats()
    assert stats["connected"] is True
    assert stats["degraded"] is False
    db.close()
