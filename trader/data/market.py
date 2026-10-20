from __future__ import annotations

"""Market data: provider abstraction, paginated + retried fetch, normalized symbols.

Bar shape: {"ts": int ms, "o","h","l","c","v": float}. Symbols normalized to BASE/QUOTE.
"""

import threading
import time
from collections import OrderedDict
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

from .redis_cache import RedisCache

TF_MS = {"1Min": 60_000, "5Min": 300_000, "15Min": 900_000, "1H": 3_600_000, "1D": 86_400_000}


def canon(sym: str) -> str:
    s = str(sym or "").strip().upper()
    if "/" in s:
        return s
    for q in ("USDC", "USDT", "USD", "BTC"):
        if len(s) > len(q) and s.endswith(q):
            return s[:-len(q)] + "/" + q
    return s


class Provider(ABC):
    name = "base"

    @abstractmethod
    def fetch(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> list[dict]:
        ...


def _retry(fn, attempts: int = 3, base: float = 1.0, jitter: float = 0.5):
    """Retry transient failures with exponential backoff + jitter.

    Permanent failures (auth, invalid symbol, malformed response) are raised
    immediately; callers must not retry orders blindly.
    """
    import random
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i + 1 >= attempts:
                break
            # sleep with jitter so concurrent callers don't thundering-herd
            time.sleep(base * 2 ** i + random.uniform(0, jitter))
    raise last  # type: ignore[misc]


class AlpacaProvider(Provider):
    name = "alpaca"

    def __init__(self, key: str = "", secret: str = "", client=None) -> None:
        self._key, self._secret, self._client = key, secret, client

    def _c(self):
        if self._client is not None:
            return self._client
        from alpaca.data.historical import CryptoHistoricalDataClient
        return CryptoHistoricalDataClient(self._key, self._secret)

    def fetch(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> list[dict]:
        from datetime import datetime, timedelta, timezone
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        tf = {"1Min": TimeFrame(1, TimeFrameUnit.Minute), "5Min": TimeFrame(5, TimeFrameUnit.Minute),
              "15Min": TimeFrame(15, TimeFrameUnit.Minute), "1H": TimeFrame.Hour,
              "1D": TimeFrame.Day}.get(timeframe, TimeFrame(15, TimeFrameUnit.Minute))
        client, out = self._c(), []
        cur = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
        last_ts = None
        pages = 0
        max_pages = max(1, (end_ms - start_ms) // TF_MS.get(timeframe, 900_000) + 2)
        while cur < end and pages < max_pages:
            pages += 1
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=cur, end=end,
                                    limit=1000, sort="asc")
            page = _retry(lambda: client.get_crypto_bars(req))
            bars = page[symbol] if hasattr(page, "__getitem__") else []
            if not bars:
                break
            # guard: if timestamp doesn't advance, stop to avoid infinite loop
            new_last = bars[-1].timestamp
            if last_ts is not None and new_last <= last_ts:
                break
            last_ts = new_last
            for b in bars:
                out.append({"ts": int(b.timestamp.timestamp() * 1000), "o": float(b.open),
                            "h": float(b.high), "l": float(b.low), "c": float(b.close),
                            "v": float(b.volume or 0.0)})
            cur = bars[-1].timestamp + timedelta(milliseconds=1)
            if len(bars) < 1000:
                break
        return [b for b in out if b["ts"] <= end_ms]


class CcxtProvider(Provider):
    name = "ccxt"

    def __init__(self, exchange=None, exchange_id: str = "coinbase") -> None:
        self._ex, self._id = exchange, exchange_id

    def _e(self):
        if self._ex is not None:
            return self._ex
        try:
            import ccxt
        except ImportError as exc:
            # ccxt is an OPTIONAL dependency (default provider is Alpaca).
            # Fail with an actionable message instead of a bare ImportError.
            raise RuntimeError(
                "DATA_PROVIDER=ccxt requires the optional 'ccxt' package. "
                "Install it with: pip install ccxt"
            ) from exc
        return getattr(ccxt, self._id)({"enableRateLimit": True})

    def fetch(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> list[dict]:
        ex = self._e()
        tf = {"1Min": "1m", "5Min": "5m", "15Min": "15m", "1H": "1h", "1D": "1d"}.get(timeframe, "15m")
        out, since = [], start_ms
        last_ts = None
        pages = 0
        max_pages = max(1, (end_ms - start_ms) // TF_MS.get(timeframe, 900_000) + 2)
        while since < end_ms and pages < max_pages:
            pages += 1
            batch = _retry(lambda: ex.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=1000))
            if not batch:
                break
            if last_ts is not None and batch[-1][0] <= last_ts:
                break
            last_ts = batch[-1][0]
            for r in batch:
                out.append({"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                            "l": float(r[3]), "c": float(r[4]), "v": float(r[5])})
            since = batch[-1][0] + 1
            if len(batch) < 1000:
                break
        return [b for b in out if b["ts"] <= end_ms]


class Cache:
    """Bounded TTL cache with LRU eviction."""

    def __init__(self, max_entries: int = 1000, ttl: int = 300) -> None:
        self.max, self.ttl = max_entries, ttl
        self._d: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, k: str):
        with self._lock:
            hit = self._d.get(k)
            if hit is None or hit[0] <= time.time():
                self._d.pop(k, None)
                self.misses += 1
                return None
            # move to end = most recently used
            self._d.move_to_end(k)
            self.hits += 1
            return hit[1]

    def put(self, k: str, v: object) -> None:
        with self._lock:
            now = time.time()
            if k in self._d:
                self._d.move_to_end(k)
            self._d[k] = (now + self.ttl, v)
            for dead in [kk for kk, (e, _) in self._d.items() if e <= now]:
                del self._d[dead]
            while len(self._d) > self.max:
                self._d.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._d), "hits": self.hits, "misses": self.misses}


class MarketStore:
    """Cached + concurrent multi-symbol fetch. One of the main V1 speed fixes.

    Uses RedisCache (Redis-backed with in-memory fallback) so cached bars
    survive across server restarts when Redis is configured.
    """

    def __init__(self, provider: Provider, cache: RedisCache, workers: int = 8) -> None:
        self.p, self.cache, self.workers = provider, cache, workers

    def bars(self, symbol: str, timeframe: str, limit: int = 150) -> list[dict]:
        sym = canon(symbol)
        key = f"{sym}|{timeframe}|{limit}"
        hit = self.cache.get(key)
        if hit is not None:
            return hit  # type: ignore[return-value]
        tfms = TF_MS.get(timeframe, 900_000)
        now = int(time.time() * 1000)
        out = self.p.fetch(sym, timeframe, now - tfms * (limit + 5), now)[-limit:]
        self.cache.put(key, out)
        return out

    def many(self, symbols: list[str], timeframe: str, limit: int = 150) -> dict[str, list[dict] | Exception]:
        res: dict[str, list[dict] | Exception] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            futs = {ex.submit(self._safe, s, timeframe, limit): s for s in symbols}
            for f, s in futs.items():
                res[s] = f.result()
        return res

    def _safe(self, s: str, tf: str, limit: int):
        try:
            return self.bars(s, tf, limit)
        except Exception as exc:  # noqa: BLE001 - per-symbol failure must not kill the batch
            return exc
