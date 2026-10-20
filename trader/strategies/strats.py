from __future__ import annotations

"""Six independent strategies. Each emits signal/confidence/evidence or None."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Sig:
    name: str
    symbol: str
    ts: int
    signal: float
    conf: float
    feat: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    invalid: str = ""


def _c(x: float) -> float:
    return max(-1.0, min(1.0, x))


class Trend:
    name = "trend"

    def run(self, sym: str, f: list[dict]) -> Sig | None:
        if len(f) < 51 or f[-1]["s20"] is None or f[-1]["s50"] is None:
            return None
        last = f[-1]
        gap = (last["s20"] - last["s50"]) / last["c"] if last["c"] else 0.0
        mn = _c((last["macd"] or 0.0) / (last["c"] * 0.01) if last["c"] else 0.0)
        w = [x["c"] for x in f[-21:-1]]
        bo = 1.0 if last["c"] > max(w) else (-1.0 if last["c"] < min(w) else 0.0)
        s = _c(0.5 * _c(gap * 50) + 0.3 * mn + 0.2 * bo)
        return Sig("trend", sym, last["ts"], round(s, 4),
                   round(min(1.0, abs(gap) * 40 + abs(mn) * 0.3 + (0.2 if bo else 0)), 4),
                   {"gap": round(gap, 6), "bo": bo},
                   f"SMA20 {'above' if last['c'] > last['s20'] else 'below'} SMA50",
                   "Invalid if MA gap flips next candle.")


class Momentum:
    name = "momentum"

    def run(self, sym: str, f: list[dict]) -> Sig | None:
        if len(f) < 15:
            return None
        last = f[-1]
        if last["r12"] is None or last["r1"] is None:
            return None
        vol = last["sd"] or (abs(last["c"]) * 0.01) or 1e-9
        nm = _c(last["r12"] / (vol / last["c"] * 3.464) if last["c"] else 0.0)
        decay = 0.5 if abs(last["r1"]) > 0.03 else 1.0
        return Sig("momentum", sym, last["ts"], round(_c(nm * decay), 4),
                   round(min(1.0, abs(nm) * 0.8), 4), {"r12": round(last["r12"], 4)},
                   f"12-bar return {last['r12']:.4f}", "Invalid if return flips sign.")


class MeanRevert:
    name = "mean_reversion"

    def __init__(self, entry: float = 1.5) -> None:
        self.entry = entry

    def run(self, sym: str, f: list[dict]) -> Sig | None:
        if len(f) < 22:
            return None
        last, z = f[-1], f[-1]["z"]
        if z is None:
            return None
        cf = 0.2 if (z > 0 and (last["rsi"] or 50) > 60) or (z < 0 and (last["rsi"] or 50) < 40) else 0.0
        if abs(z) < self.entry:
            s, c = 0.0, max(0.0, 1 - abs(z) / self.entry) * 0.3
        else:
            s = _c(-z / 3.0 + (-0.2 if z > 0 else 0.2) + (cf if z < 0 else -cf))
            c = min(1.0, (abs(z) - self.entry) / 2 + 0.4 + cf)
        return Sig("mean_reversion", sym, last["ts"], round(s, 4), round(c, 4),
                   {"z": round(z, 3)}, f"z={z:.2f} vs {self.entry}", "Invalid if z crosses zero.")


class Volatility:
    name = "volatility"

    def run(self, sym: str, f: list[dict]) -> Sig | None:
        if len(f) < 25:
            return None
        vols = [x["sd"] / x["c"] for x in f[-25:] if x["sd"] and x["c"]]
        if not vols:
            return None
        ratio = vols[-1] / (sorted(vols)[len(vols) // 2] or 1e-12)
        reg = "low" if ratio < 0.7 else ("high" if ratio > 1.5 else "normal")
        med = sorted(vols)[len(vols) // 2] or 1e-12
        return Sig("volatility", sym, f[-1]["ts"], 0.0, round(min(1.0, abs(ratio - 1)), 4),
                   {"regime": reg, "shock": vols[-1] > 2 * med,
                    "mult": round(max(0.25, min(1.5, 1.0 / max(ratio, 0.25))), 3)},
                   f"Regime={reg}", "Invalid if median shifts >25%.")


class VolRegime:
    name = "volume_regime"

    def run(self, sym: str, f: list[dict]) -> Sig | None:
        if len(f) < 55:
            return None
        last = f[-1]
        cl = [x["c"] for x in f[-50:]]
        rng = max(cl) - min(cl)
        trending = (abs(cl[-1] - cl[0]) / rng) > 0.5 if rng else False
        pos = _c((last["c"] - min(cl)) / (rng or 1e-12) * 2 - 1)
        anom = last["vr"] > 2.0
        s = _c(pos * 0.4 + (0.2 if trending and last["c"] > f[-2]["c"] else 0.0))
        return Sig("volume_regime", sym, last["ts"], round(s, 4),
                   round(min(1.0, (0.5 if anom else 0.2) + (0.3 if trending else 0.1)), 4),
                   {"regime": "trend" if trending else "range", "anom": anom},
                   f"Regime={'trend' if trending else 'range'}", "Invalid on unconfirmed flip.")


def _corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    da = [x - ma for x in a]
    db = [x - mb for x in b]
    den = (sum(x * x for x in da) * sum(x * x for x in db)) ** 0.5
    return sum(x * y for x, y in zip(da, db)) / den if den else 0.0


class StatRel:
    """Co-movement monitor only. Never arbitrage, never assumed persistent."""
    name = "stat_relationship"

    def __init__(self, ref: dict[str, list[float]] | None = None) -> None:
        self.ref = ref or {}

    def run(self, sym: str, f: list[dict]) -> Sig | None:
        if len(f) < 30 or sym not in self.ref or len(self.ref[sym]) < 30:
            return None
        c = _corr([x["c"] for x in f[-30:]], self.ref[sym][-30:])
        bd = abs(c) < 0.3
        return Sig("stat_relationship", sym, f[-1]["ts"], round(_c(c * 0.3), 4),
                   0.7 if bd else 0.4, {"corr": round(c, 3), "breakdown": bd},
                   f"corr={c:.2f}{' BREAKDOWN' if bd else ''}", "Invalid if sign flips.")


ALL = [Trend(), Momentum(), MeanRevert(), Volatility(), VolRegime(), StatRel()]
