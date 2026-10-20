from __future__ import annotations

"""Alpaca paper-only execution. Live URLs rejected. Fills reconciled, never assumed."""

import time
import uuid
import math
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


class Broker:
    def __init__(self, key: str, secret: str, url: str, orders_on: bool, client=None,
                 broker_environment: str = "paper") -> None:
        self.url = require_paper_endpoint(url)
        if broker_environment != "paper":
            raise PermissionError("Broker environment must be explicitly paper.")
        self._key, self._secret = key, secret
        self._broker_environment = broker_environment
        self.orders_on = orders_on
        self._client = client
        self.history: list[Fill] = []

    def _c(self):
        if self._client is not None:
            return self._client
        from alpaca.trading.client import TradingClient
        return TradingClient(self._key, self._secret, paper=True, url_override=self.url)

    def account(self) -> dict:
        a = self._c().get_account()
        equity = float(a.equity)
        last_equity = getattr(a, "last_equity", None)
        if last_equity is None:
            raise RuntimeError("Broker daily P&L cannot be verified.")
        return {"equity": equity, "bp": float(a.buying_power),
                "day_pnl": equity - float(last_equity),
                "status": str(getattr(a.status, "value", a.status)),
                "pdt": bool(getattr(a, "pattern_day_trader", False))}

    def positions(self) -> dict[str, float]:
        return {canon(p.symbol): float(p.market_value or 0.0) for p in self._c().get_all_positions()}

    def positions_detail(self) -> list[dict]:
        out: list[dict] = []
        for p in self._c().get_all_positions():
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
        return sorted(a.symbol for a in self._c().get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO))
                      if getattr(a, "tradable", False))

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
        c = self._c()
        verify_paper_order_submission(c, self._key, self._secret, self.url, self._broker_environment)
        cid = cid or f"tr-{uuid.uuid4().hex[:12]}"
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
            f = Fill(cid, "unknown", symbol, side, kw.get("qty"), 0.0, None, f"submit failed: {type(exc).__name__}")
            self.history.append(f)
            return f
        time.sleep(0.2)
        try:
            o = c.get_order_by_client_id(cid)
            fq = float(o.filled_qty or 0)
            st = str(getattr(o.status, "value", o.status)).lower()
            status = "filled" if fq > 0 and st in ("filled", "partially_filled", "fill") else st
            f = Fill(cid, status, symbol, side, kw.get("qty"), fq,
                     float(o.filled_avg_price) if o.filled_avg_price else None, "")
        except Exception:  # noqa: BLE001
            f = Fill(cid, "unknown", symbol, side, kw.get("qty"), 0.0, None, "submitted; fill unconfirmed")
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
            c = self._c()
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = c.get_orders(req)
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
