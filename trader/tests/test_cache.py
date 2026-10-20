from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock

from trader.data.redis_cache import RedisCache
from trader.data.db_store import DBStore


def test_cache_put_get_delete():
    c = RedisCache(max_entries=10, ttl=60, redis_url="")  # no Redis -> memory fallback
    c.put("foo", 42)
    assert c.get("foo") == 42
    c.delete("foo")
    assert c.get("foo") is None


def test_cache_ttl_expiry():
    c = RedisCache(max_entries=10, ttl=1, redis_url="")
    c.put("foo", 42)
    assert c.get("foo") == 42
    import time; time.sleep(1.1)
    assert c.get("foo") is None


def test_cache_lru_eviction():
    c = RedisCache(max_entries=3, ttl=1000, redis_url="")
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    c.put("d", 4)  # should evict "a"
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3
    assert c.get("d") == 4


def test_db_store_memory_fallback():
    db = DBStore(database_url="")  # no DB -> memory fallback
    db.save_signal("BTC/USD", {"action": "BUY", "confidence": 0.8, "score": 0.5})
    db.save_backtest("BTC/USD", "15Min", {"total_return": 0.05}, {"fee": 0.0025})
    db.save_order("BTC/USD", "BUY", 100.0, "filled", "abc123")
    stats = db.stats()
    assert stats["backend"] == "memory"
    assert stats["signals"] == 1
    assert stats["backtests"] == 1
    assert stats["orders"] == 1


def test_cache_stats():
    c = RedisCache(max_entries=10, ttl=60, redis_url="")
    c.put("a", 1)
    stats = c.stats()
    assert stats["backend"] == "memory"


@patch("redis.Redis.from_url")
def test_redis_health_check_ok(mock_from_url):
    """PING succeeds -> health ok, no URL leaked."""
    client = MagicMock()
    client.ping.return_value = True
    mock_from_url.return_value = client
    c = RedisCache(redis_url="redis://user:secret@host:6379/0")
    hc = c.health_check()
    assert hc["status"] == "ok"
    assert hc["latency_ms"] is not None
    assert hc["error"] is None
    assert "secret" not in str(hc)
    c.close()
    client.close.assert_called_once()


@patch("redis.Redis.from_url")
def test_redis_health_check_not_available(mock_from_url):
    """PING fails during init -> not available, health reports error."""
    client = MagicMock()
    client.ping.side_effect = Exception("connection refused: user:secret@host:6379")
    mock_from_url.return_value = client
    c = RedisCache(redis_url="redis://user:secret@host:6379/0")
    assert c.available is False
    assert c._client is None
    hc = c.health_check()
    assert hc["status"] == "error"
    assert hc["error"] == "redis not available"
    assert "secret" not in str(hc)
    c.close()


def test_redis_health_check_no_url():
    """No REDIS_URL -> not available, no false claim of connection."""
    c = RedisCache(redis_url="")
    assert c.available is False
    hc = c.health_check()
    assert hc["status"] == "error"
    assert hc["error"] == "redis not available"


def test_redis_close_marks_unavailable():
    """close() releases the client and marks Redis unavailable."""
    c = RedisCache(redis_url="")
    c._redis_available = True
    mock_client = MagicMock()
    c._client = mock_client
    c.close()
    mock_client.close.assert_called_once()
    assert c._client is None
    assert c.available is False


@patch("redis.Redis.from_url")
def test_redis_health_check_ping_fail_sanitized(mock_from_url):
    """PING fails in health_check -> error sanitized, password not leaked."""
    client = MagicMock()
    client.ping.side_effect = Exception("timeout: user:secret@host:6379")
    mock_from_url.return_value = client
    c = RedisCache(redis_url="redis://user:secret@host:6379/0")
    # Force the client to appear available so health_check calls PING.
    c._redis_available = True
    c._client = client
    hc = c.health_check()
    assert hc["status"] == "error"
    assert hc["error"] is not None
    assert "secret" not in hc["error"]
    assert "user:secret" not in hc["error"]
    assert c.available is False
    c.close()


class _FakeRedis:
    """In-memory fake redis client used to exercise serialization round-trips
    without a live Redis service or any credentials."""
    def __init__(self) -> None:
        self.store = {}

    def ping(self):
        return True

    def get(self, k):
        return self.store.get(k)

    def setex(self, k, ttl, v):
        self.store[k] = v

    def delete(self, k):
        self.store.pop(k, None)

    def close(self):
        pass

    def info(self, section):
        return {}


@patch("redis.Redis.from_url")
def test_redis_round_trip_serialization(mock_from_url):
    """put/get/delete round-trip through JSON serialization; no live Redis."""
    mock_from_url.return_value = _FakeRedis()
    c = RedisCache(redis_url="redis://user:secret@host:6379/0")
    assert c.available is True
    payload = {"a": [1, 2, 3], "b": None, "c": "x"}
    c.put("obj", payload)
    assert c.get("obj") == payload
    c.delete("obj")
    assert c.get("obj") is None
    c.close()


@patch("redis.Redis.from_url")
def test_redis_get_failure_falls_back_to_local(mock_from_url):
    """get/setex raising mid-operation flips available=False and serves from local cache."""
    client = _FakeRedis()

    def boom_get(k):
        raise RuntimeError("conn reset: user:secret@host")

    def boom_setex(k, ttl, v):
        raise RuntimeError("conn reset: user:secret@host")

    client.get = boom_get
    client.setex = boom_setex
    mock_from_url.return_value = client
    c = RedisCache(redis_url="redis://user:secret@host:6379/0")
    assert c.available is True
    # get raises -> flips to False, falls back to local (None)
    assert c.get("missing") is None
    assert c.available is False
    # put raises -> stores locally; subsequent reads served from local
    c.put("k", 1)
    assert c.available is False
    assert c.get("k") == 1
    # error text is sanitized (no password leaked)
    hc = c.health_check()
    assert hc["status"] == "error"
    assert "secret" not in str(hc)
    assert "user:secret" not in str(hc)
    c.close()
