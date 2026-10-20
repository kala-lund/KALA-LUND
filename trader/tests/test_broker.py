from __future__ import annotations

import math
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.execution.broker import Broker, PAPER_HOST, Fill


def test_broker_rejects_live_url():
    try:
        Broker("k", "s", "https://api.alpaca.markets", False)
        assert False, "live URL must be rejected"
    except ValueError:
        pass


def test_broker_paper_url_ok():
    b = Broker("k", "s", "https://paper-api.alpaca.markets", False)
    assert b.url == "https://paper-api.alpaca.markets"


def test_broker_disabled_raises():
    b = Broker("k", "s", "https://paper-api.alpaca.markets", False)
    try:
        b.send("BTC/USD", "BUY", notional=10)
        assert False, "disabled paper orders must raise"
    except PermissionError:
        pass


def test_broker_fill_parsing():
    class FakeAccount:
        status = "ACTIVE"

    class FakeOrder:
        status = type("S", (), {"value": "filled"})()
        filled_qty = 0.001
        filled_avg_price = 50000.0

    class FakeClient:
        _base_url = "https://paper-api.alpaca.markets"
        _sandbox = True

        def get_account(self):
            return FakeAccount()
        def submit_order(self, req):
            return None
        def get_order_by_client_id(self, cid):
            return FakeOrder()

    b = Broker("k", "s", "https://paper-api.alpaca.markets", True, client=FakeClient())
    f = b.send("BTC/USD", "BUY", notional=50, cid="t1")
    assert f.status == "filled"
    assert f.filled_qty == 0.001
    assert f.filled_price == 50000.0
    assert len(b.history) == 1


def test_paper_host_constant():
    assert PAPER_HOST == "paper-api.alpaca.markets"


def test_broker_rejects_missing_credentials_before_submit():
    class FakeClient:
        _base_url = "https://paper-api.alpaca.markets"
        _sandbox = True

        def get_account(self):
            raise AssertionError("account must not be queried with missing credentials")

    b = Broker("", "s", "https://paper-api.alpaca.markets", True, client=FakeClient())
    try:
        b.send("BTC/USD", "BUY", notional=10)
        assert False, "missing credentials must reject"
    except PermissionError:
        pass


def test_broker_rejects_unverified_account_without_submit():
    class FakeClient:
        _base_url = "https://paper-api.alpaca.markets"
        _sandbox = True
        submitted = False

        def get_account(self):
            return type("Account", (), {"status": "INACTIVE"})()

        def submit_order(self, req):
            self.submitted = True

    client = FakeClient()
    b = Broker("k", "s", "https://paper-api.alpaca.markets", True, client=client)
    try:
        b.send("BTC/USD", "BUY", notional=10)
        assert False, "inactive account must reject"
    except PermissionError:
        pass
    assert not client.submitted


def test_broker_rejects_live_configured_injected_client():
    class FakeClient:
        _base_url = "https://api.alpaca.markets"
        _sandbox = False
        submitted = False

        def get_account(self):
            return type("Account", (), {"status": "ACTIVE"})()

        def submit_order(self, req):
            self.submitted = True

    client = FakeClient()
    b = Broker("k", "s", "https://paper-api.alpaca.markets", True, client=client)
    try:
        b.send("BTC/USD", "BUY", notional=10)
        assert False, "live-configured injected client must reject"
    except PermissionError:
        pass
    assert not client.submitted


def test_broker_rejects_non_paper_environment():
    try:
        Broker("k", "s", "https://paper-api.alpaca.markets", True, broker_environment="live")
        assert False, "live environment must reject"
    except PermissionError:
        pass


def test_broker_rejects_non_finite_order_values_before_submission():
    class FakeClient:
        _base_url = "https://paper-api.alpaca.markets"
        _sandbox = True
        submitted = False

        def get_account(self):
            return type("Account", (), {"status": "ACTIVE"})()

        def submit_order(self, req):
            self.submitted = True

    for kwargs in ({"qty": math.inf}, {"qty": math.nan}, {"notional": math.inf}, {"notional": math.nan}):
        client = FakeClient()
        broker = Broker("k", "s", "https://paper-api.alpaca.markets", True, client=client)
        try:
            broker.send("BTC/USD", "BUY", **kwargs)
            assert False, f"non-finite order must reject: {kwargs}"
        except ValueError:
            pass
        assert not client.submitted


def test_broker_rejects_both_qty_and_notional():
    broker = Broker("k", "s", "https://paper-api.alpaca.markets", True, client=object())
    try:
        broker.send("BTC/USD", "BUY", qty=1, notional=10)
        assert False, "both qty and notional must reject"
    except ValueError:
        pass
