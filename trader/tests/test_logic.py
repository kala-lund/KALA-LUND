from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.backtest.bt import run as bt_run, windows
from trader.engine.loop import combine
from trader.features.feat import features
from trader.risk.guard import Account, Limits, Risk
from trader.strategies.strats import ALL, StatRel

from .test_core_data import bars


def test_strategies_schema_and_repro():
    f = features(bars(90))
    assert features(bars(90)) == f
    made = []
    for t in ALL:
        s = t.run("BTC/USD", f)
        if s is None:
            continue
        assert -1.0 <= s.signal <= 1.0 and 0.0 <= s.conf <= 1.0 and s.reason and s.invalid
        made.append(s)
    assert len(made) >= 5
    r = StatRel(ref={"BTC/USD": [x["c"] * 1.01 for x in f]}).run("BTC/USD", f)
    assert r is not None and r.feat["corr"] > 0.9
    a = combine(made, {})
    assert a.action in ("BUY", "SELL", "HOLD", "ABSTAIN")


def test_aggregator_abstains():
    f = features(bars(90))
    a = [t.run("BTC/USD", f) for t in ALL if t.run("BTC/USD", f)]
    opp = type(a[0])(name=a[0].name, symbol=a[0].symbol, ts=a[0].ts, signal=-a[0].signal,
                     conf=1.0, feat={}, reason="x", invalid="y")
    assert combine([a[0], opp], {}, max_dis=0.1).action == "ABSTAIN"
    old = type(a[0])(name=a[0].name, symbol=a[0].symbol, ts=1, signal=1.0,
                     conf=1.0, feat={}, reason="x", invalid="y")
    assert combine([old], {}, max_age_s=1).action == "ABSTAIN"


def _acct(**kw):
    d = {"equity": 10000.0, "bp": 10000.0, "positions": {}, "pending": 0,
         "day_pnl": 0.0, "peak": 10000.0, "data_age_s": 5.0}
    d.update(kw)
    return Account(**d)


def test_risk_and_kill_and_dupes():
    g = Risk(Limits())
    assert g.check("BTC/USD", "BUY", 100, _acct(), "a1").ok
    assert not g.check("BTC/USD", "BUY", 10 ** 9, _acct(), "a2").ok
    assert not g.check("BTC/USD", "BUY", 100, None, "a3").ok
    assert not g.check("BTC/USD", "BUY", 100, _acct(data_age_s=9999), "a4").ok
    assert not g.check("BTC/USD", "BUY", 100, _acct(), "a1").ok
    g.trip("t")
    assert g.killed and not g.check("BTC/USD", "BUY", 1, _acct(), "a5").ok
    g.reset()
    assert not g.killed


def test_backtest_and_windows():
    b = [{"ts": i, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 5} for i in range(60)]
    r = bt_run(b, [0.5] * 60)
    assert r.metrics["trades"] >= 1 and r.metrics["fees"] > 0 and len(r.curve) == 60
    w = windows(1000, 400, 200, 200, 200)
    assert w and all(x.test[1] <= 1000 for x in w)
    assert windows(100, 400, 200, 200, 200) == []
