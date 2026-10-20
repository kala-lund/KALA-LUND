from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.backtest.bt import run, windows, Win


def test_zero_price_skipped_no_crash():
    bars = [{"ts": i, "o": 100, "h": 101, "l": 99, "c": 100 + i, "v": 5} for i in range(10)]
    bars.append({"ts": 10, "o": 0, "h": 0, "l": 0, "c": 0, "v": 5})  # invalid price
    bars += [{"ts": 11 + i, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 5}
             for i in range(5)]
    w = [0.5] * len(bars)
    r = run(bars, w)
    assert r.metrics["equity"] > 0
    assert r.metrics["trades"] >= 0
    assert len(r.curve) == len([b for b in bars if float(b["c"]) > 0])


def test_negative_equity_no_math_domain():
    bars = [{"ts": i, "o": 100, "h": 101, "l": 99, "c": 100, "v": 5} for i in range(10)]
    # 2x leverage → cash can go negative on a drop; equity floored at 0
    w = [2.0] * 10
    r = run(bars, w)
    assert all(e >= 0 for e in r.curve)
    assert r.metrics["equity"] >= 0


def test_constant_weight_price_no_churn():
    bars = [{"ts": i, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 5} for i in range(20)]
    w = [0.5] * 20
    r = run(bars, w, fee=0.0, slip=0.0)  # zero costs isolate phantom churn
    # constant price + constant weight → exactly 1 trade (initial), no phantom churn
    assert r.metrics["trades"] == 1, f"expected 1 trade, got {r.metrics['trades']}"
    assert r.metrics["fees"] == 0.0


def test_drawdown_and_sharpe_sanity():
    bars = [{"ts": i, "o": 100.0 + i, "h": 101 + i, "l": 99 + i, "c": 100.0 + i, "v": 5}
            for i in range(50)]
    w = [0.5] * 50
    r = run(bars, w, ppy=365 * 96)
    assert r.metrics["max_dd"] >= 0.0
    assert r.metrics["max_dd"] <= 1.0
    assert isinstance(r.metrics["sharpe"], float)


def test_walkforward_windows_bounds():
    ws = windows(1000, 400, 200, 200, 200)
    assert len(ws) == 2
    for w in ws:
        assert w.train[0] < w.train[1] <= w.val[0] < w.val[1] <= w.test[0] < w.test[1] <= 1000
    # expanding variant
    we = windows(1000, 400, 200, 200, 200, expanding=True)
    assert len(we) == 2
    assert all(w.train[0] == 0 for w in we)
    # insufficient data → empty
    assert windows(100, 400, 200, 200, 200) == []


def test_win_dataclass_fields():
    w = Win((0, 100), (100, 200), (200, 300))
    assert w.train == (0, 100) and w.val == (100, 200) and w.test == (200, 300)
