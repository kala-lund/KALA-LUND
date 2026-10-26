from __future__ import annotations

"""Redis-backed cache with fallback to in-memory if Redis unavailable.

Used for fast market-data caching, signal caching, and rate-limit counters.
Falls back gracefully to a dict-based cache when Redis is not configured
or unreachable, so the system never hard-depends on Redis.
"""

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from ..core.kit import safe_msg


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class RedisCache:
    """Bounded TTL cache with Redis backend + in-memory fallback.

    If REDIS_URL is set and redis is installed, uses Redis. Otherwise falls
    back to a local LRU cache so the system keeps working without Redis.
    """

    def __init__(self, max_entries: int = 1000, ttl: int = 300,
                 redis_url: str = "", reconnect_cooldown: float = 30.0) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self.redis_url = redis_url
        self._client: Any = None
        self._local: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._lock = threading.Lock()
        self._redis_available = False
        # A dropped Redis must be recoverable: instead of disabling Redis for the
        # whole process lifetime, a reconnect is attempted (rate-limited) on the
        # next cache operation once this cooldown has elapsed.
        self._reconnect_cooldown = max(0.0, float(reconnect_cooldown))
        self._last_reconnect_attempt = 0.0
        self._init_redis()

    def _init_redis(self) -> None:
        if not self.redis_url:
            return
        try:
            import redis  # type: ignore
            client = redis.Redis.from_url(
                self.redis_url, decode_responses=True, socket_timeout=2,
                socket_connect_timeout=2)
            client.ping()
            self._client = client
            self._redis_available = True
        except Exception:  # noqa: BLE001 - Redis optional, fall back to local
            self._client = None
            self._redis_available = False
        self._last_reconnect_attempt = time.time()

    def _maybe_reconnect(self) -> None:
        """Attempt a reconnect at most once per cooldown window.

        Never raises; keeps Redis disabled (and the local cache authoritative)
        until a ping succeeds. No URL or credential is logged.
        """
        if self._redis_available or not self.redis_url:
            return
        now = time.time()
        if now - self._last_reconnect_attempt < self._reconnect_cooldown:
            return
        self._last_reconnect_attempt = now
        try:
            import redis  # type: ignore
            client = redis.Redis.from_url(
                self.redis_url, decode_responses=True, socket_timeout=2,
                socket_connect_timeout=2)
            client.ping()
            self._client = client
            self._redis_available = True
        except Exception:  # noqa: BLE001 - stay on the local cache
            self._client = None
            self._redis_available = False

    @property
    def available(self) -> bool:
        return self._redis_available

    def health_check(self) -> dict:
        """Return a sanitized, independent health report for Redis.

        Uses PING. Never includes the connection URL, password, or raw
        error text. Does not claim Redis is connected when it is not.
        """
        if not self._redis_available or self._client is None:
            return {"status": "error", "latency_ms": None,
                    "error": "redis not available"}
        import time
        start = time.perf_counter()
        try:
            self._client.ping()
            return {"status": "ok", "latency_ms": round(
                (time.perf_counter() - start) * 1000, 2), "error": None}
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            self._redis_available = False
            return {"status": "error", "latency_ms": None,
                    "error": safe_msg(exc)}

    def close(self) -> None:
        """Close the Redis client and release resources."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            self._client = None
        self._redis_available = False

    def _key(self, k: str) -> str:
        return f"qtrade:{k}"

    def get(self, k: str) -> Any:
        if not self._redis_available:
            self._maybe_reconnect()
        if self._redis_available and self._client is not None:
            try:
                raw = self._client.get(self._key(k))
                if raw is None:
                    return None
                return json.loads(raw)
            except Exception:  # noqa: BLE001
                self._redis_available = False
        with self._lock:
            e = self._local.get(k)
            if e is None or e.expires_at <= time.time():
                self._local.pop(k, None)
                return None
            self._local.move_to_end(k)
            return e.value

    def put(self, k: str, v: Any, ttl: int | None = None) -> None:
        ttl = ttl if ttl is not None else self.ttl
        if not self._redis_available:
            self._maybe_reconnect()
        if self._redis_available and self._client is not None:
            try:
                self._client.set(
                    self._key(k), json.dumps(v, default=str), ex=ttl)
                return
            except Exception:  # noqa: BLE001
                self._redis_available = False
        with self._lock:
            now = time.time()
            if k in self._local:
                self._local.move_to_end(k)
            self._local[k] = CacheEntry(v, now + ttl)
            while len(self._local) > self.max_entries:
                self._local.popitem(last=False)

    def delete(self, k: str) -> None:
        if not self._redis_available:
            self._maybe_reconnect()
        if self._redis_available and self._client is not None:
            try:
                self._client.delete(self._key(k))
                return
            except Exception:  # noqa: BLE001
                self._redis_available = False
        with self._lock:
            self._local.pop(k, None)

    def stats(self) -> dict:
        if self._redis_available and self._client is not None:
            try:
                info = self._client.info("memory")
                return {"backend": "redis", "used_memory": info.get("used_memory_human"),
                        "connected_clients": info.get("connected_clients")}
            except Exception:  # noqa: BLE001
                self._redis_available = False
        with self._lock:
            return {"backend": "memory", "entries": len(self._local),
                    "max_entries": self.max_entries}
