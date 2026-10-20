from __future__ import annotations

"""Independent risk guard. Final authority on every order. Fail-closed."""

import math
import threading
from dataclasses import dataclass, field


@dataclass
class Limits:
    max_order: float = 500.0
    max_position: float = 2000.0
    max_exposure: float = 5000.0
    max_conc: float = 0.5
    max_daily_loss: float = 300.0
    max_drawdown: float = 0.15
    max_open: int = 10
    min_bp: float = 100.0
    stale_s: int = 900


@dataclass
class Account:
    equity: float
    bp: float
    positions: dict[str, float]
    pending: int = 0
    day_pnl: float = 0.0
    peak: float = 0.0
    data_age_s: float = 0.0


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)


class Risk:
    def __init__(self, lim: Limits) -> None:
        self.lim = lim
        self._kill = False
        self._reason = ""
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._id_order: list[str] = []

    def trip(self, reason: str = "") -> None:
        with self._lock:
            self._kill, self._reason = True, reason

    def reset(self) -> None:
        with self._lock:
            self._kill, self._reason = False, ""

    @property
    def killed(self) -> bool:
        with self._lock:
            return self._kill

    def check(self, symbol: str, side: str, notional: float, acct: Account | None, cid: str = "") -> Verdict:
        if self.killed:
            return Verdict(False, ["Kill switch engaged."])
        if acct is None:
            return Verdict(False, ["Account unknown; failing closed."])
        r: list[str] = []
        try:
            order_notional = float(notional)
        except (TypeError, ValueError):
            order_notional = float("nan")
        if side not in ("BUY", "SELL"):
            r.append(f"Bad side {side!r}.")
        if not math.isfinite(order_notional) or not (order_notional > 0):
            r.append("Notional must be positive and finite.")
        if cid:
            with self._lock:
                if cid in self._ids:
                    return Verdict(False, [f"Duplicate order {cid}."])
        L = self.lim
        cur = abs(acct.positions.get(symbol, 0.0))
        if math.isfinite(order_notional) and order_notional > L.max_order:
            r.append(f"Order {order_notional:.2f} > max {L.max_order:.2f}.")
        if math.isfinite(order_notional) and cur + order_notional > L.max_position:
            r.append(f"Position {cur + order_notional:.2f} > max {L.max_position:.2f}.")
        if math.isfinite(order_notional) and sum(abs(v) for v in acct.positions.values()) + order_notional > L.max_exposure:
            r.append("Exposure cap breached.")
        if math.isfinite(order_notional) and acct.equity > 0 and (cur + order_notional) / acct.equity > L.max_conc:
            r.append("Concentration cap breached.")
        if acct.day_pnl <= -abs(L.max_daily_loss):
            r.append(f"Daily loss hit ({acct.day_pnl:.2f}).")
        if acct.peak > 0 and (acct.peak - acct.equity) / acct.peak > L.max_drawdown:
            r.append("Max drawdown breached.")
        if acct.pending >= L.max_open:
            r.append("Too many open orders.")
        if acct.bp < L.min_bp:
            r.append("Insufficient buying power.")
        if math.isfinite(order_notional) and order_notional > acct.bp:
            r.append(f"Order notional {order_notional:.2f} exceeds buying power {acct.bp:.2f}.")
        if acct.data_age_s > L.stale_s:
            r.append(f"Stale data ({acct.data_age_s:.0f}s).")
        if r:
            return Verdict(False, r)
        if cid:
            with self._lock:
                self._ids.add(cid)
                self._id_order.append(cid)
                while len(self._ids) > 5000:
                    old = self._id_order.pop(0)
                    self._ids.discard(old)
        return Verdict(True, ["All checks passed."])

    def check_exit(self, symbol: str, side: str, notional: float,
                   acct: Account | None, cid: str = "", position_side: str = "long",
                   quantity: float | None = None, position_quantity: float | None = None) -> Verdict:
        """Validate a reduce-only exit without treating it as new exposure."""
        if self.killed:
            return Verdict(False, ["Kill switch engaged."])
        if acct is None:
            return Verdict(False, ["Account unknown; failing closed."])
        reasons: list[str] = []
        try:
            order_notional = float(notional)
        except (TypeError, ValueError):
            order_notional = float("nan")
        if quantity is not None:
            try:
                order_quantity = float(quantity)
            except (TypeError, ValueError):
                order_quantity = float("nan")
            if not math.isfinite(order_quantity) or order_quantity <= 0:
                reasons.append("Exit quantity must be positive and finite.")
            if (position_quantity is not None and math.isfinite(order_quantity)
                    and order_quantity > position_quantity):
                reasons.append("Exit quantity exceeds current position quantity.")
        expected_side = "BUY" if str(position_side).lower() == "short" else "SELL"
        if side != expected_side:
            reasons.append(f"Exit side must be {expected_side} for the current position.")
        if not math.isfinite(order_notional) or order_notional <= 0:
            reasons.append("Notional must be positive and finite.")
        current = abs(float(acct.positions.get(symbol, 0.0) or 0.0))
        if current <= 0:
            reasons.append("No current position for reduce-only exit.")
        elif math.isfinite(order_notional) and order_notional > current:
            reasons.append("Exit exceeds current position value.")
        if math.isfinite(order_notional) and order_notional > self.lim.max_order:
            reasons.append(f"Order {order_notional:.2f} > max {self.lim.max_order:.2f}.")
        if acct.pending >= self.lim.max_open:
            reasons.append("Too many open orders.")
        if acct.data_age_s > self.lim.stale_s:
            reasons.append(f"Stale data ({acct.data_age_s:.0f}s).")
        if cid:
            with self._lock:
                if cid in self._ids:
                    return Verdict(False, [f"Duplicate order {cid}."])
        if reasons:
            return Verdict(False, reasons)
        if cid:
            with self._lock:
                self._ids.add(cid)
                self._id_order.append(cid)
                while len(self._ids) > 5000:
                    old = self._id_order.pop(0)
                    self._ids.discard(old)
        return Verdict(True, ["All exit checks passed."])
