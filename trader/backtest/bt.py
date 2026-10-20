from __future__ import annotations

"""Backtest (no look-ahead: execute at signal-bar close + slippage) + walk-forward windows."""

import math
from dataclasses import dataclass


@dataclass
class BTResult:
    curve: list[float]
    trades: list[dict]
    metrics: dict
    assumptions: dict


def _sharpe(rets: list[float], ppy: int | None = None) -> float:
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    sd = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
    if ppy is None:
        ppy = 1
    return (m / (sd or 1e-12)) * math.sqrt(ppy)


def run(bars: list[dict], weights: list[float], cash: float = 10_000.0,
        fee: float = 0.0025, slip: float = 0.0005,
        ppy: int | None = None) -> BTResult:
    """Backtest with no look-ahead: execute at signal-bar close + slippage.

    Prices are validated: zero/negative prices are skipped (no fills), equity is
    floored at zero so log returns never take math.log of a non-positive number.
    Execution and valuation use the same slippage-adjusted price so a constant
    weight/price does not churn (no phantom fees).
    """
    cl: list[float] = []
    for b in bars:
        try:
            px = float(b["c"])
        except (TypeError, ValueError, KeyError):
            continue
        if px <= 0:
            continue  # skip invalid/zero prices; never fill at zero
        cl.append(px)
    if not cl:
        return BTResult([], [], {"total_return": 0.0, "sharpe": 0.0, "max_dd": 0.0,
                                 "trades": 0, "fees": 0.0, "equity": cash},
                        {"exec": "signal-bar close + slippage", "fee": fee, "slip": slip,
                         "note": "No valid price bars; not evidence of future profit."})
    cash_, sh, curve, trades, costs, peak = cash, 0.0, [], [], 0.0, cash
    for i, px0 in enumerate(cl):
        w = weights[i] if i < len(weights) else 0.0
        px = px0  # execute at mid; slippage charged as cost below
        eq = max(cash_ + sh * px0, 0.0)
        tgt = w * eq
        dv = tgt - sh * px
        if abs(dv) > 1e-9:
            c = abs(dv) * (fee + slip)  # fee + slippage as cost on traded notional
            cash_ = max(cash_ - dv - c, 0.0)
            sh = tgt / px
            costs += c
            trades.append({"bar": i, "price": round(px, 4), "dnot": round(dv, 2), "fee": round(c, 4)})
        eq = max(cash_ + sh * px0, 0.0)
        curve.append(round(eq, 2))
        peak = max(peak, eq)
    rets = [math.log(curve[i] / curve[i - 1]) for i in range(1, len(curve))
            if curve[i - 1] > 0 and curve[i] > 0]
    pk, dd = 0.0, 0.0
    for e in curve:
        pk = max(pk, e)
        dd = max(dd, (pk - e) / pk if pk else 0.0)
    metrics = {"total_return": round(curve[-1] / cash - 1, 6) if curve else 0.0,
               "sharpe": round(_sharpe(rets, ppy), 4), "max_dd": round(dd, 6),
               "trades": len(trades), "fees": round(costs, 2),
               "equity": curve[-1] if curve else cash}
    return BTResult(curve, trades, metrics,
                    {"exec": "signal-bar close + slippage", "fee": fee, "slip": slip,
                     "note": "Not evidence of future profit."})


@dataclass
class Win:
    train: tuple[int, int]
    val: tuple[int, int]
    test: tuple[int, int]


def windows(n: int, tr: int, va: int, te: int, step: int, expanding: bool = False) -> list[Win]:
    out, s = [], 0
    while True:
        t0 = 0 if expanding else s
        if s + tr + va + te > n:
            break
        out.append(Win((t0, t0 + tr), (s + tr, s + tr + va), (s + tr + va, s + tr + va + te)))
        s += step
    return out
