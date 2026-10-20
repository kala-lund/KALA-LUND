from __future__ import annotations

"""Past-only technical features. Zero-guarded throughout (V1 vol_ratio lesson)."""

import math


def _sma(v: list[float], n: int):
    out: list[float | None] = [None] * len(v)
    acc = 0.0
    for i, x in enumerate(v):
        acc += x
        if i >= n:
            acc -= v[i - n]
        if i >= n - 1:
            out[i] = acc / n
    return out


def _ema(v: list[float], n: int):
    out: list[float | None] = [None] * len(v)
    k, prev = 2 / (n + 1), None
    for i, x in enumerate(v):
        prev = x if prev is None else x * k + prev * (1 - k)
        if i >= n - 1:
            out[i] = prev
    return out


def _rsi(c: list[float], n: int = 14):
    out: list[float | None] = [None] * len(c)
    g = l = 0.0
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
        if i >= n:
            o = c[i - n + 1] - c[i - n]
            g -= max(o, 0)
            l -= max(-o, 0)
        if i >= n:
            out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def _sd(v: list[float], n: int):
    out: list[float | None] = [None] * len(v)
    for i in range(len(v)):
        if i >= n - 1:
            w = v[i - n + 1:i + 1]
            m = sum(w) / n
            out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


def features(bars: list[dict]) -> list[dict]:
    closes = [float(b["c"]) for b in bars]
    vols = [float(b.get("v", 0.0) or 0.0) for b in bars]
    s20, s50, e12, e26, rsi, sd20 = _sma(closes, 20), _sma(closes, 50), _ema(closes, 12), _ema(closes, 26), _rsi(closes), _sd(closes, 20)
    rows = []
    for i, b in enumerate(bars):
        macd = (e12[i] - e26[i]) if e12[i] is not None and e26[i] is not None else None
        c = closes[i]
        r1 = (c / closes[i - 1] - 1) if i > 0 and closes[i - 1] else None
        r12 = (c / closes[i - 12] - 1) if i >= 12 and closes[i - 12] else None
        z = ((c - s20[i]) / sd20[i]) if (s20[i] is not None and sd20[i]) else None
        vw = vols[max(0, i - 19):i + 1]
        vd = sum(vw) / min(20, i + 1)
        vr = vols[i] / vd if (i > 0 and vd) else 1.0
        rows.append({"ts": b["ts"], "c": c, "s20": s20[i], "s50": s50[i], "macd": macd,
                     "rsi": rsi[i], "r1": r1, "r12": r12, "z": z, "vr": vr, "sd": sd20[i]})
    return rows
