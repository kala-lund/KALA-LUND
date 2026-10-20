from __future__ import annotations

import math
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.risk.guard import Account, Limits, Risk, Verdict


def _acct(**kw):
    d = {"equity": 10000.0, "bp": 10000.0, "positions": {}, "pending": 0,
         "day_pnl": 0.0, "peak": 10000.0, "data_age_s": 5.0}
    d.update(kw)
    return Account(**d)


def test_risk_approves_normal_order():
    g = Risk(Limits())
    assert g.check("BTC/USD", "BUY", 100, _acct(), "n1").ok


def test_risk_rejects_exceeds_order_max():
    g = Risk(Limits())
    assert not g.check("BTC/USD", "BUY", 600, _acct(), "n2").ok


def test_risk_rejects_exceeds_position_cap():
    g = Risk(Limits())
    acct = _acct(positions={"BTC/USD": 1900.0})
    assert not g.check("BTC/USD", "BUY", 200, acct, "n3").ok


def test_risk_rejects_exceeds_exposure_cap():
    g = Risk(Limits())
    acct = _acct(positions={"BTC/USD": 2500.0, "ETH/USD": 2600.0})
    assert not g.check("SOL/USD", "BUY", 100, acct, "n4").ok


def test_risk_rejects_exceeds_concentration():
    g = Risk(Limits())
    acct = _acct(equity=1000.0, positions={"BTC/USD": 600.0})
    assert not g.check("BTC/USD", "BUY", 100, acct, "n5").ok


def test_risk_rejects_exceeds_buying_power():
    g = Risk(Limits(max_order=5000.0))
    acct = _acct(bp=100.0)
    assert not g.check("BTC/USD", "BUY", 500, acct, "n6").ok


def test_risk_rejects_kill_switch():
    g = Risk(Limits())
    g.trip("manual")
    assert g.killed
    assert not g.check("BTC/USD", "BUY", 10, _acct(), "n7").ok
    g.reset()
    assert not g.killed
    assert g.check("BTC/USD", "BUY", 10, _acct(), "n8").ok


def test_risk_rejects_unknown_account():
    g = Risk(Limits())
    assert not g.check("BTC/USD", "BUY", 10, None, "n9").ok


def test_risk_rejects_stale_data():
    g = Risk(Limits())
    assert not g.check("BTC/USD", "BUY", 10, _acct(data_age_s=9999), "n10").ok


def test_risk_rejects_duplicate_id():
    g = Risk(Limits())
    assert g.check("BTC/USD", "BUY", 10, _acct(), "dup").ok
    assert not g.check("BTC/USD", "BUY", 10, _acct(), "dup").ok


def test_risk_rejects_non_finite_notional():
    g = Risk(Limits())
    assert not g.check("BTC/USD", "BUY", math.nan, _acct(), "nan").ok
    assert not g.check("BTC/USD", "BUY", math.inf, _acct(), "inf").ok


def test_risk_daily_loss_and_drawdown():
    g = Risk(Limits())
    assert not g.check("BTC/USD", "BUY", 10, _acct(day_pnl=-301.0), "n11").ok
    assert not g.check("BTC/USD", "BUY", 10, _acct(peak=10000.0, equity=8400.0), "n12").ok
