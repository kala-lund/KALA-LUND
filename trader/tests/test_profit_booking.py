from __future__ import annotations

"""Take-profit (profit booking) tests for the automation loop.

Covers: take-profit disabled/None, threshold triggering, below-threshold skip,
at-threshold trigger, zero-notional skip, kill-switch blocks take-profit,
execution error does not crash the loop, and repeated cycles re-attempt exits.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from types import SimpleNamespace

from trader.engine.loop import Loop
from trader.execution.broker import Broker
from trader.risk.guard import Account, Limits, Risk


class FakeBroker:
    def __init__(self, positions=None, send_side_effect=None, send_status="filled"):
        self.positions_list = positions or []
        self.sent = []
        self.send_side_effect = send_side_effect
        self.send_status = send_status

    def positions_detail(self):
        return [{**position, "side": position.get("side", "long")} for position in self.positions_list]

    def send(self, sym, side, **kw):
        if self.send_side_effect:
            raise self.send_side_effect
        self.sent.append((sym, side, kw.get("notional"), kw.get("cid")))
        return SimpleNamespace(status=self.send_status)


def _s(take_profit_pct=0.0, **kw):
    d = {"crypto_symbols": ("BTC/USD",), "automate_dry_run": False,
         "automate_minutes": 15, "automate_min_conf": 0.5,
         "automate_max_notional": 100.0, "enable_paper_orders": True,
         "max_position_notional": 2000.0, "take_profit_pct": take_profit_pct}
    d.update(kw)
    return SimpleNamespace(**d)


def _pipe(halted=False):
    return {"halted": halted, "signals": {}, "allocation": {"notionals": {}}}


def _acct():
    return Account(10000.0, 10000.0, {"BTC/USD": 500.0}, 0, 0.0, 10000.0, 5.0)


def test_take_profit_disabled():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}])
    lo = Loop(_s(take_profit_pct=0.0), _pipe, Risk(Limits()), fb, _acct)
    r = lo.once()
    assert fb.sent == []  # no sells when tp disabled
    assert r["action"] == "cycled"


def test_take_profit_none():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}])
    lo = Loop(_s(take_profit_pct=None), _pipe, Risk(Limits()), fb, _acct)
    lo.once()
    assert fb.sent == []


def test_take_profit_triggers_above_threshold():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}])
    lo = Loop(_s(take_profit_pct=2.0), _pipe, Risk(Limits()), fb, _acct)
    lo.once()
    assert len(fb.sent) == 1
    assert fb.sent[0][0] == "BTC/USD"
    assert fb.sent[0][1] == "SELL"
    assert fb.sent[0][2] == 500.0  # notional = market_value


def test_take_profit_below_threshold_no_sell():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 1.0,
                                "market_value": 500.0}])
    lo = Loop(_s(take_profit_pct=2.0), _pipe, Risk(Limits()), fb, _acct)
    lo.once()
    assert fb.sent == []


def test_take_profit_at_threshold_triggers():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 2.0,
                                "market_value": 300.0}])
    lo = Loop(_s(take_profit_pct=2.0), _pipe, Risk(Limits()), fb, _acct)
    lo.once()
    assert len(fb.sent) == 1
    assert fb.sent[0][1] == "SELL"


def test_take_profit_zero_notional_skipped():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 0.0}])
    lo = Loop(_s(take_profit_pct=2.0), _pipe, Risk(Limits()), fb, _acct)
    lo.once()
    assert fb.sent == []


def test_take_profit_kill_switch_blocks():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}])
    risk = Risk(Limits())
    risk.trip("manual")
    lo = Loop(_s(take_profit_pct=2.0), _pipe, risk, fb, _acct)
    r = lo.once()
    assert r["action"] == "halted"
    assert fb.sent == []  # take-profit never runs when kill switch engaged


def test_take_profit_execution_error_does_not_crash():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}],
                    send_side_effect=RuntimeError("broker down"))
    lo = Loop(_s(take_profit_pct=2.0), _pipe, Risk(Limits()), fb, _acct)
    r = lo.once()
    # loop should not crash; take-profit errors are swallowed
    assert r["action"] == "cycled"


def test_take_profit_deduplicates_until_position_reconciles():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}])
    lo = Loop(_s(take_profit_pct=2.0), _pipe, Risk(Limits()), fb, _acct)
    lo.once()
    lo.once()
    assert len(fb.sent) == 1
    assert any("pending reconciliation" in str(item) for item in lo.once()["results"])


def test_take_profit_uses_real_paper_broker_safety_boundary():
    class FakePosition:
        symbol = "BTC/USD"
        side = "long"
        qty = 0.01
        market_value = 500.0
        cost_basis = 450.0
        unrealized_pl = 50.0
        unrealized_plpc = 0.05
        current_price = 50000.0

    class FakeOrder:
        status = type("Status", (), {"value": "accepted"})()
        filled_qty = 0.0
        filled_avg_price = None

    class FakeClient:
        _base_url = "https://paper-api.alpaca.markets"
        _sandbox = True
        submitted = False
        submitted_request = None

        def get_account(self):
            return type("Account", (), {"status": "ACTIVE"})()

        def get_all_positions(self):
            return [FakePosition()]

        def submit_order(self, request):
            self.submitted = True
            self.submitted_request = request

        def get_order_by_client_id(self, client_order_id):
            return FakeOrder()

    client = FakeClient()
    broker = Broker("k", "s", "https://paper-api.alpaca.markets", True, client=client)
    lo = Loop(_s(take_profit_pct=2.0, automate_dry_run=False, enable_paper_orders=True),
              _pipe, Risk(Limits()), broker, _acct)
    result = lo.once()
    assert result["action"] == "cycled"
    assert client.submitted
    assert float(client.submitted_request.qty) == 0.01
    assert client.submitted_request.notional is None


def test_take_profit_covers_short_position_with_buy():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "side": "short", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0, "qty": 0.01}])
    lo = Loop(_s(take_profit_pct=2.0), _pipe, Risk(Limits()), fb, _acct)
    lo.once()
    assert fb.sent and fb.sent[0][1] == "BUY"


def test_take_profit_retries_after_rejected_order():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}], send_status="rejected")
    lo = Loop(_s(take_profit_pct=2.0), _pipe, Risk(Limits()), fb, _acct)
    lo.once()
    lo.once()
    assert len(fb.sent) == 2


def test_take_profit_dry_run_never_calls_broker():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}])
    risk = Risk(Limits())
    lo = Loop(_s(take_profit_pct=2.0, automate_dry_run=True, enable_paper_orders=True),
              _pipe, risk, fb, _acct)
    result = lo.once()
    assert fb.sent == []
    assert not risk.killed
    assert result["status"]["dry_orders"] == 1


def test_take_profit_without_paper_orders_never_calls_broker():
    fb = FakeBroker(positions=[{"symbol": "BTC/USD", "unrealized_pl_pct": 5.0,
                                "market_value": 500.0}])
    risk = Risk(Limits())
    lo = Loop(_s(take_profit_pct=2.0, automate_dry_run=False, enable_paper_orders=False),
              _pipe, risk, fb, _acct)
    lo.once()
    assert fb.sent == []
    assert not risk.killed
