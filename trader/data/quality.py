from __future__ import annotations

"""Bar validation. Reports problems; never invents replacement data."""

import statistics
import time


def check(bars: list[dict], timeframe_ms: int, stale_seconds: int = 900) -> dict:
    if not bars:
        return {"ok": False, "rows": 0, "issues": ["empty series"], "gaps": 0,
                "dups": 0, "outliers": 0, "stale": True}
    issues: list[str] = []
    for i, b in enumerate(bars):
        for k in ("ts", "o", "h", "l", "c", "v"):
            if k not in b or b[k] is None:
                issues.append(f"row {i} missing {k}")
                break
    ts = [b["ts"] for b in bars if isinstance(b.get("ts"), (int, float))]
    dups = len(ts) - len(set(ts))
    if dups:
        issues.append(f"{dups} duplicate timestamps")
    gaps, ordered = 0, sorted(ts)
    for a, b in zip(ordered, ordered[1:]):
        if timeframe_ms > 0 and (b - a) > timeframe_ms * 1.5:
            gaps += 1
    if gaps:
        issues.append(f"{gaps} missing-candle gaps")
    outliers = 0
    closes = [float(b["c"]) for b in bars if isinstance(b.get("c"), (int, float))]
    if closes:
        m, sd = statistics.fmean(closes), statistics.pstdev(closes) or 1e-12
        outliers = sum(1 for c in closes if abs(c - m) > 8 * sd)
        for b in bars:
            try:
                if not (float(b["l"]) <= min(float(b["o"]), float(b["c"]))
                        and max(float(b["o"]), float(b["c"])) <= float(b["h"])):
                    outliers += 1
                    break
            except (TypeError, ValueError):
                outliers += 1
                break
    if outliers:
        issues.append(f"{outliers} outlier rows")
    stale = (time.time() * 1000 - ordered[-1]) > stale_seconds * 1000 if ordered else True
    if stale:
        issues.append("data is stale")
    return {"ok": not issues, "rows": len(bars), "issues": issues, "gaps": gaps,
            "dups": dups, "outliers": outliers, "stale": stale}
