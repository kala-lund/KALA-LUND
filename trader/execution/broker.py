from __future__ import annotations

"""Alpaca paper-only execution. Live URLs rejected. Fills reconciled, never assumed."""

import time
import uuid
import math
import threading
from concurrent.futures import (
    CancelledError,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeout,
)
from dataclasses import dataclass

from ..data.market import canon
from ..core.kit import safe_msg
from .safety import (
    PAPER_TRADING_ENDPOINT,
    verify_paper_order_submission,
    require_paper_endpoint,
)

PAPER_HOST = "paper-api.alpaca.markets"
PAPER_URL = PAPER_TRADING_ENDPOINT


def _utc_iso() -> str:
    """UTC timestamp with second precision (used for in-process fill records)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _install_request_timeout(client: object, timeout: float) -> None:
    """Bound every HTTP call made by an alpaca-py client.

    ``alpaca/common/rest.py`` calls ``self._session.request(method, url, **opts)``
    without a ``timeout``, so a half-open connection can block a worker thread
    forever. A ``Future.result(timeout=...)`` cannot cancel that thread; after a
    few such calls the read pool is permanently starved. Injecting a per-request
    ``(connect, read)`` timeout makes the socket itself give up.

    Read-only verbs use ``timeout``; order submission (POST/DELETE/PATCH) gets a
    longer bound because abandoning a submit creates ambiguous order state. Both
    stay bounded -- an unbounded submit would hang the request thread forever.
    """
    session = getattr(client, "_session", None)
    if session is None or not callable(getattr(session, "request", None)):
        return
    if getattr(session, "_finrl_timeout_installed", False):
        return
    original = session.request
    connect = min(5.0, max(1.0, float(timeout)))
    read = max(1.0, float(timeout))
    submit_read = max(30.0, read)

    def request(method, url, **kwargs):
        verb = str(method).upper()
        kwargs.setdefault("timeout", (connect, read if verb in ("GET", "HEAD") else submit_read))
        return original(method, url, **kwargs)

    session.request = request
    session._finrl_timeout_installed = True


def _shutdown_pool(pool: ThreadPoolExecutor) -> None:
    """Shut a pool down without waiting (``cancel_futures`` needs Python 3.9+)."""
    try:
        pool.shutdown(wait=False, cancel_futures=True)
        return
    except TypeError:  # pragma: no cover - Python 3.7/3.8
        pass
    except Exception:  # noqa: BLE001 - best-effort cleanup
        return
    try:  # pragma: no cover - Python 3.7/3.8
        pool.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class Fill:
    cid: str
    status: str
    symbol: str
    side: str
    qty: float | None
    filled_qty: float = 0.0
    filled_price: float | None = None
    note: str = ""
    ts: str = ""


class Broker:
    def __init__(self, key: str, secret: str, url: str, orders_on: bool, client=None,
                 broker_environment: str = "paper", read_timeout: float = 10.0) -> None:
        self.url = require_paper_endpoint(url)
        if broker_environment != "paper":
            raise PermissionError("Broker environment must be explicitly paper.")
        self._key, self._secret = key, secret
        self._broker_environment = broker_environment
        self.orders_on = orders_on
        self._client = client
        # Bounded execution history: this is an in-process convenience view, not
        # durable state (orders are persisted through DBStore).
        from collections import deque
        self.history: deque = deque(maxlen=1000)
        # Hard upper bound for READ-ONLY broker calls (see _read).
        self._read_timeout = max(0.1, float(read_timeout))
        self._read_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="broker-read")
        # One memoized alpaca-py client per calling thread: reusing the client
        # keeps the TLS connection alive (a cold handshake alone can exceed the
        # read bound) without sharing a requests.Session across threads.
        self._local = threading.local()
        self._pool_lock = threading.Lock()

    def close(self) -> None:
        """Release the read-only worker pool (idempotent)."""
        with self._pool_lock:
            # Replace before shutting down so a late read gets a usable pool
            # instead of "cannot schedule new futures after shutdown".
            old, self._read_pool = self._read_pool, ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="broker-read")
        _shutdown_pool(old)

    def _reset_read_pool(self) -> None:
        """Replace a read pool whose worker cannot be interrupted.

        ``Future.cancel()`` cannot stop a thread already blocked in a socket
        call, so a timed-out read is abandoned and the pool is replaced. Without
        this, four slow calls would make every later read time out even after
        the network recovers.
        """
        with self._pool_lock:
            old, self._read_pool = self._read_pool, ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="broker-read")
        _shutdown_pool(old)

    def _c(self):
        if self._client is not None:
            return self._client
        cached = getattr(self._local, "client", None)
        if cached is not None:
            return cached
        from alpaca.trading.client import TradingClient
        client = TradingClient(self._key, self._secret, paper=True, url_override=self.url)
        _install_request_timeout(client, self._read_timeout)
        self._local.client = client
        return client

    def _read(self, label: str, fn):
        """Run a READ-ONLY broker call with a bounded latency.

        alpaca-py builds its client on ``requests.Session()`` and never passes a
        timeout to it (``alpaca/common/rest.py``: ``_one_request`` calls
        ``self._session.request(method, url, **opts)``), so a hung or half-open
        connection can block the calling request thread indefinitely -- and
        ``/api/v2/summary`` calls this synchronously. Reads are safe to abandon
        because every caller already degrades gracefully on failure.

        Order submission is deliberately NOT wrapped this way: a timeout there
        would create ambiguous order state, which the execution path handles
        explicitly (status ``unknown`` -> kill switch). Submission is bounded by
        the per-request socket timeout instead (see _install_request_timeout).
        """
        deadline = time.perf_counter() + self._read_timeout
        for _attempt in (1, 2):
            with self._pool_lock:
                pool = self._read_pool
                try:
                    fut = pool.submit(fn)
                except RuntimeError:
                    # A concurrent close/reset retired this pool; retry on the
                    # replacement instead of failing the caller.
                    fut = None
            if fut is None:
                continue
            try:
                return fut.result(timeout=max(0.0, deadline - time.perf_counter()))
            except FuturesTimeout as exc:
                if fut.done():
                    # The callable itself raised TimeoutError (3.11+ aliases
                    # concurrent.futures.TimeoutError); the pool is healthy.
                    raise
                fut.cancel()
                # The worker is likely still blocked in the socket; retire the
                # pool so later reads are not queued behind a dead thread.
                self._reset_read_pool()
                raise TimeoutError(
                    f"Broker read '{label}' exceeded {self._read_timeout:.1f}s"
                ) from exc
            except CancelledError:
                # Cancelled by a concurrent pool reset before it started.
                continue
        raise TimeoutError(f"Broker read '{label}' could not be scheduled")

    def account(self) -> dict:
        a = self._read("account", lambda: self._c().get_account())
        equity = float(a.equity)
        last_equity = getattr(a, "last_equity", None)
        if last_equity is None:
            raise RuntimeError("Broker daily P&L cannot be verified.")
        return {"equity": equity, "bp": float(a.buying_power),
                "cash": float(getattr(a, "cash", 0.0) or 0.0),
                "long_market_value": float(getattr(a, "long_market_value", 0.0) or 0.0),
                "day_pnl": equity - float(last_equity),
                "status": str(getattr(a.status, "value", a.status)),
                "pdt": bool(getattr(a, "pattern_day_trader", False))}

    def positions(self) -> dict[str, float]:
        rows = self._read("positions", lambda: self._c().get_all_positions())
        return {canon(p.symbol): float(p.market_value or 0.0) for p in rows}

    def positions_detail(self) -> list[dict]:
        out: list[dict] = []
        for p in self._read("positions_detail", lambda: self._c().get_all_positions()):
            symbol = canon(p.symbol)
            mv = float(p.market_value or 0.0)
            cost = float(p.cost_basis or 0.0)
            up = float(p.unrealized_pl or 0.0)
            up_pct = float(p.unrealized_plpc or 0.0) * 100.0
            cur = float(p.current_price or 0.0)
            out.append({"symbol": symbol, "side": str(getattr(p.side, "value", p.side)).lower(),
                        "qty": float(p.qty or 0.0), "market_value": mv,
                        "cost_basis": cost, "unrealized_pl": up, "unrealized_pl_pct": up_pct,
                        "current_price": cur})
        return out

    def crypto(self) -> list[str]:
        from alpaca.trading.enums import AssetClass
        from alpaca.trading.requests import GetAssetsRequest
        assets = self._read(
            "crypto",
            lambda: self._c().get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO)),
        )
        return sorted(a.symbol for a in assets if getattr(a, "tradable", False))

    def send(self, symbol: str, side: str, qty: float = 0.0, notional: float = 0.0,
             kind: str = "market", limit: float | None = None, cid: str = "") -> Fill:
        if not self.orders_on:
            raise PermissionError("Paper orders disabled (dry-run only).")
        if side not in ("BUY", "SELL"):
            raise ValueError(f"Bad side {side!r}.")
        if kind not in ("market", "limit"):
            raise ValueError(f"Bad order type {kind!r}.")
        try:
            order_qty = float(qty)
            order_notional = float(notional)
        except (TypeError, ValueError) as exc:
            raise ValueError("qty and notional must be numeric.") from exc
        if not math.isfinite(order_qty) or order_qty < 0:
            raise ValueError("qty must be finite and non-negative.")
        if not math.isfinite(order_notional) or order_notional < 0:
            raise ValueError("notional must be finite and non-negative.")
        if order_qty > 0 and order_notional > 0:
            raise ValueError("Provide exactly one of qty or notional.")
        if order_qty <= 0 and order_notional <= 0:
            raise ValueError("qty or notional required.")
        if kind == "limit":
            try:
                order_limit = float(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError("limit must be numeric for limit orders.") from exc
            if not math.isfinite(order_limit) or order_limit <= 0:
                raise ValueError("limit must be positive and finite.")
        # The socket timeout installed by _install_request_timeout bounds this
        # account read, so a hung broker cannot block the request thread. One
        # client is used for verification and submission (same TLS session).
        c = self._c()
        verify_paper_order_submission(c, self._key, self._secret, self.url, self._broker_environment)
        cid = cid or f"tr-{uuid.uuid4().hex[:12]}"
        submitted_at = _utc_iso()
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        kw: dict = {"symbol": symbol, "side": OrderSide.BUY if side == "BUY" else OrderSide.SELL,
                    "time_in_force": TimeInForce.GTC, "client_order_id": cid}
        if order_notional > 0:
            kw["notional"] = round(order_notional, 2)
        else:
            kw["qty"] = order_qty
        req = LimitOrderRequest(limit_price=round(order_limit, 2), **kw) if kind == "limit" else MarketOrderRequest(**kw)
        try:
            c.submit_order(req)
        except Exception as exc:  # noqa: BLE001
            f = Fill(cid, "unknown", symbol, side, kw.get("qty"), 0.0, None, f"submit failed: {type(exc).__name__}", submitted_at)
            self.history.append(f)
            return f
        time.sleep(0.2)
        try:
            o = c.get_order_by_client_id(cid)
            fq = float(o.filled_qty or 0)
            st = str(getattr(o.status, "value", o.status)).lower()
            status = "filled" if fq > 0 and st in ("filled", "partially_filled", "fill") else st
            f = Fill(cid, status, symbol, side, kw.get("qty"), fq,
                     float(o.filled_avg_price) if o.filled_avg_price else None, "", submitted_at)
        except Exception:  # noqa: BLE001
            f = Fill(cid, "unknown", symbol, side, kw.get("qty"), 0.0, None, "submitted; fill unconfirmed", submitted_at)
        self.history.append(f)
        return f

    def get_open_orders(self):
        """Return (open_orders, error). Never submits/cancels/replaces orders.

        Queries Alpaca for OPEN orders and normalizes each record. On API
        failure returns ([], error_string) so callers can report incomplete
        reconciliation rather than falsely claiming success.
        """
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self._read("open_orders", lambda: self._c().get_orders(req))
            out = []
            for o in orders:
                out.append({
                    "client_order_id": getattr(o, "client_order_id", None),
                    "symbol": getattr(o, "symbol", ""),
                    "side": str(getattr(o.side, "value", o.side)).lower(),
                    "notional": float(getattr(o, "notional", 0) or 0),
                    "qty": float(getattr(o.qty, "value", o.qty) or 0)
                    if getattr(o, "qty", None) else 0.0,
                    "status": str(getattr(o.status, "value", o.status)).lower(),
                    "filled_qty": float(getattr(o.filled_qty, "value", o.filled_qty) or 0),
                })
            return out, None
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            return [], f"broker open-orders query failed: {safe_msg(exc)}"
