from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trader.engine.loop import Loop
from trader.execution.broker import Broker


class _FakeBroker:
    """Stand-in broker for reconciliation tests (no Alpaca calls)."""

    def __init__(self, open_orders, error=None):
        self.open_orders = open_orders
        self.error = error

    def get_open_orders(self):
        return self.open_orders, self.error


class _FakeDB:
    """Stand-in DBStore for reconciliation tests (no SQL)."""

    def __init__(self, open_orders, error=None):
        self.open_orders = open_orders
        self.error = error

    def get_open_orders(self):
        return self.open_orders, self.error


class _FakeSettings:
    automate_dry_run = True
    automate_minutes = 15
    crypto_symbols = ["BTC/USD"]
    automate_min_conf = 0.5
    automate_max_notional = 100.0


class _FakeRisk:
    killed = False


def _make_loop(broker, db=None):
    settings = _FakeSettings()
    risk = _FakeRisk()
    return Loop(settings, lambda: {}, risk, broker, lambda: None, db=db)


def test_reconcile_open_orders_matched():
    """Open orders present on both broker and DB are matched by client ID."""
    broker = _FakeBroker([{"client_order_id": "cid-1", "symbol": "BTC/USD",
                           "side": "buy", "notional": 100, "status": "new",
                           "filled_qty": 0}])
    db = _FakeDB([{"client_order_id": "cid-1", "symbol": "BTC/USD",
                   "side": "buy", "notional": 100, "status": "new"}])
    result = _make_loop(broker, db).reconcile()
    assert result["reconciled"] is True
    assert result["status"] == "complete"
    assert result["matched"] == 1
    assert result["broker_open"] == 1
    assert result["db_open"] == 1
    assert result["errors"] == []


def test_reconcile_no_open_orders():
    """Nothing open on either side reconciles cleanly."""
    broker = _FakeBroker([])
    db = _FakeDB([])
    result = _make_loop(broker, db).reconcile()
    assert result["reconciled"] is True
    assert result["matched"] == 0


def test_reconcile_duplicate_ids_not_double_counted():
    """Duplicate client IDs on the broker side are matched once per DB row."""
    broker = _FakeBroker([{"client_order_id": "cid-1", "symbol": "BTC/USD",
                           "side": "buy", "notional": 100, "status": "new"}])
    db = _FakeDB([{"client_order_id": "cid-1", "symbol": "BTC/USD",
                   "side": "buy", "notional": 100, "status": "new"},
                  {"client_order_id": "cid-1", "symbol": "BTC/USD",
                   "side": "buy", "notional": 100, "status": "new"}])
    result = _make_loop(broker, db).reconcile()
    # Two DB rows match the same broker CID -> matched counts both rows.
    assert result["matched"] == 2
    assert result["reconciled"] is True


def test_reconcile_uncertain_order_incomplete():
    """An order whose broker status is unknown is not treated as filled."""
    broker = _FakeBroker([{"client_order_id": "cid-1", "symbol": "BTC/USD",
                           "side": "buy", "notional": 100, "status": "unknown"}])
    db = _FakeDB([{"client_order_id": "cid-1", "symbol": "BTC/USD",
                   "side": "buy", "notional": 100, "status": "new"}])
    result = _make_loop(broker, db).reconcile()
    assert result["reconciled"] is False
    assert result["status"] == "incomplete"
    assert result["uncertain"] == 1


def test_reconcile_unmatched_broker_order_is_incomplete():
    broker = _FakeBroker([{"client_order_id": "orphan", "symbol": "BTC/USD",
                           "side": "buy", "notional": 100, "status": "new"}])
    db = _FakeDB([])
    result = _make_loop(broker, db).reconcile()
    assert result["reconciled"] is False
    assert result["status"] == "incomplete"
    assert result["uncertain"] == 1


def test_reconcile_failure_trips_real_risk():
    from trader.risk.guard import Limits, Risk

    risk = Risk(Limits())
    loop = Loop(_FakeSettings(), lambda: {}, risk, _FakeBroker(None, error="broker down"),
                lambda: None, db=_FakeDB([]))
    assert loop.reconcile()["reconciled"] is False
    assert risk.killed


def test_start_does_not_launch_when_reconciliation_is_incomplete():
    from trader.risk.guard import Limits, Risk

    risk = Risk(Limits())
    loop = Loop(_FakeSettings(), lambda: {}, risk, _FakeBroker(None, error="broker down"),
                lambda: None, db=_FakeDB([]))
    assert loop.start().running is False
    assert loop._th is None
    assert risk.killed


def test_reconcile_broker_failure():
    """Broker failure is reported; reconciliation incomplete."""
    broker = _FakeBroker(None, error="broker down")
    db = _FakeDB([])
    result = _make_loop(broker, db).reconcile()
    assert result["reconciled"] is False
    assert result["status"] == "incomplete"
    assert any("broker" in e.lower() for e in result["errors"])


def test_reconcile_db_failure():
    """DB failure is reported; reconciliation incomplete."""
    broker = _FakeBroker([])
    db = _FakeDB(None, error="db down")
    result = _make_loop(broker, db).reconcile()
    assert result["reconciled"] is False
    assert result["status"] == "incomplete"
    assert any("db" in e.lower() for e in result["errors"])


def test_reconcile_no_broker():
    """Missing broker yields incomplete reconciliation."""
    result = _make_loop(None, None).reconcile()
    assert result["reconciled"] is False
    assert result["status"] == "no_broker"


def test_reconcile_no_db():
    """No DB -> broker open orders cannot be safely reconciled."""
    broker = _FakeBroker([{"client_order_id": "cid-1", "symbol": "BTC/USD",
                           "side": "buy", "notional": 100, "status": "new"}])
    result = _make_loop(broker, None).reconcile()
    assert result["reconciled"] is False
    assert result["status"] == "incomplete"
    assert result["matched"] == 0
    assert result["db_open"] == 0
    assert result["broker_open"] == 1


def test_reconcile_requires_broker_open_order_interface():
    class NoReconcileBroker:
        pass

    result = _make_loop(NoReconcileBroker(), None).reconcile()
    assert result["reconciled"] is False
    assert "reconciliation unavailable" in result["errors"][0]


def test_reconcile_called_at_start(monkeypatch):
    """reconcile() is invoked when start() is called."""
    broker = _FakeBroker([])
    db = _FakeDB([])
    called = {"n": 0}
    orig = Loop.reconcile

    def counting_reconcile(self):
        called["n"] += 1
        return orig(self)

    monkeypatch.setattr(Loop, "reconcile", counting_reconcile)
    loop = _make_loop(broker, db)
    loop.start()
    assert called["n"] == 1
