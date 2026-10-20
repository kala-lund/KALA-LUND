from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock

from trader.data.db_store import DBStore


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
def test_db_store_close(mock_connect):
    """close() releases the connection; health reports no connection after."""
    conn = _make_conn_mock()
    mock_connect.return_value = conn
    db = DBStore(database_url="postgresql://user:pass@host:5432/db")
    db.close()
    conn.close.assert_called_once()
    assert db._conn is None
    hc = db.health_check()
    assert hc["status"] == "error"
    assert hc["error"] == "no connection"


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
