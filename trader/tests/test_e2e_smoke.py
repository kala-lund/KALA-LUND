from __future__ import annotations

"""End-to-end paper-trading cycle smoke test (trader/ V2 only).

Verifies the sequence with mocked market data and a mocked paper broker:
  market data -> strategy signal (via cycle_fn) -> risk validation ->
  mocked order submission -> database persistence -> reconciliation.

No real Alpaca requests are made; all external calls are mocked.
"""

from trader.engine.loop import Loop
from trader.execution.broker import Broker, Fill
from trader.risk.guard import Account, Limits, Risk, Verdict


class _FakeSettings:
    automate_dry_run = False
    automate_minutes = 15
    automate_min_conf = 0.5
    automate_max_notional = 100.0
    enable_paper_orders = True
    crypto_symbols = ["BTC/USD"]


class _FakeAccount:
    positions: dict = {}
    pending = 0
    day_pnl = 0.0


class _FakeDB:
    """Stand-in DBStore (no SQL, no real connection)."""

    def __init__(self) -> None:
        self.saved: list[tuple] = []
        self.open_orders: list[dict] = []
        self.error: str | None = None

    def save_order(self, symbol, side, notional, status, client_order_id) -> None:
        self.saved.append((symbol, side, float(notional), status, client_order_id))
        self.open_orders.append({"client_order_id": client_order_id,
                                 "symbol": symbol, "side": side,
                                 "notional": float(notional), "status": status})

    def get_open_orders(self):
        return self.open_orders, self.error


class _FakeBroker:
    """Stand-in broker (no Alpaca calls)."""

    def __init__(self, fill_status: str = "filled", error: str | None = None) -> None:
        self.sent: list[tuple] = []
        self.open_orders: list[dict] = []
        self.fill_status = fill_status
        self.error = error

    def send(self, symbol, side, notional=0.0, cid="") -> Fill:
        self.sent.append((symbol, side, float(notional), cid))
        if self.error:
            return Fill(cid, "unknown", symbol, side, None, 0.0, None, self.error)
        return Fill(cid, self.fill_status, symbol, side, None, 0.0, None, "")

    def get_open_orders(self):
        return self.open_orders, self.error


def _make_pipe(action="BUY", conf=0.9, notional=50.0, halted=False):
    return {
        "signals": {
            "BTC/USD": {"aggregate": {"action": action, "confidence": conf}},
        },
        "allocation": {"notionals": {"BTC/USD": notional}},
        "halted": halted,
    }


def _make_account() -> Account:
    return Account(
        equity=10000.0, bp=5000.0, positions={}, pending=0,
        day_pnl=0.0, peak=10000.0, data_age_s=10.0,
    )


def _make_risk(ok=True, reasons=None):
    risk = Risk(Limits(max_order=500.0, max_position=2000.0, max_exposure=5000.0,
                       max_conc=0.5, max_daily_loss=300.0, max_drawdown=0.15,
                       max_open=10, min_bp=100.0, stale_s=900))
    if not ok:
        risk.check = lambda *a, **k: Verdict(False, reasons or ["rejected"])
    return risk


def test_buy_signal_end_to_end():
    """BUY signal -> risk pass -> broker.send -> db.save_order persist."""
    broker = _FakeBroker()
    db = _FakeDB()
    risk = _make_risk(ok=True)
    loop = Loop(_FakeSettings(), lambda: _make_pipe(), risk, broker,
                lambda: _make_account(), db=db)
    res = loop.once()
    assert res["action"] == "cycled"
    # order submitted to (mocked) broker
    assert len(broker.sent) == 1
    sym, side, notional, cid = broker.sent[0]
    assert sym == "BTC/USD" and side == "BUY" and abs(notional - 50.0) < 1e-9
    # order persisted to (mocked) DB
    assert len(db.saved) == 1
    assert db.saved[0][:3] == ("BTC/USD", "BUY", 50.0)
    assert db.saved[0][4] == cid  # client_order_id echoed


def test_rejected_signal_no_order():
    """Risk rejection -> no broker.submit, no DB write."""
    broker = _FakeBroker()
    db = _FakeDB()
    risk = _make_risk(ok=False, reasons=["Daily loss hit"])
    loop = Loop(_FakeSettings(), lambda: _make_pipe(), risk, broker,
                lambda: _make_account(), db=db)
    res = loop.once()
    assert res["action"] == "cycled"
    assert broker.sent == []
    assert db.saved == []


def test_abstain_signal_no_order():
    """ABSTAIN / low confidence -> no broker.submit, no DB write."""
    broker = _FakeBroker()
    db = _FakeDB()
    risk = _make_risk(ok=True)
    loop = Loop(_FakeSettings(), lambda: _make_pipe(action="ABSTAIN", conf=0.9),
                risk, broker, lambda: _make_account(), db=db)
    res = loop.once()
    assert broker.sent == []
    assert db.saved == []


def test_reconcile_no_duplicates():
    """Reconciliation matches broker orders to local records without duplication."""
    broker = _FakeBroker()
    db = _FakeDB()
    risk = _make_risk(ok=True)
    loop = Loop(_FakeSettings(), lambda: _make_pipe(), risk, broker,
                lambda: _make_account(), db=db)
    loop.once()  # submit one order
    # Simulate broker reporting the same order back as open
    broker.open_orders = [{"client_order_id": broker.sent[0][3],
                           "symbol": "BTC/USD", "side": "buy",
                           "notional": 50.0, "status": "new",
                           "filled_qty": 0}]
    result = loop.reconcile()
    assert result["reconciled"] is True
    assert result["matched"] == 1
    assert result["db_open"] == 1
    assert result["broker_open"] == 1
    # run again: still one match, no duplicate local record
    result2 = loop.reconcile()
    assert result2["matched"] == 1
    assert len(db.saved) == 1  # no extra persistence from reconcile


def test_reconcile_broker_failure_incomplete():
    """Broker failure -> incomplete reconciliation, no false success."""
    broker = _FakeBroker(error="broker down")
    db = _FakeDB()
    risk = _make_risk(ok=True)
    loop = Loop(_FakeSettings(), lambda: _make_pipe(), risk, broker,
                lambda: _make_account(), db=db)
    result = loop.reconcile()
    assert result["reconciled"] is False
    assert result["status"] == "incomplete"
    assert any("broker" in e.lower() for e in result["errors"])


def test_no_signal_no_action():
    """No tradable signal -> no order, no persistence."""
    broker = _FakeBroker()
    db = _FakeDB()
    risk = _make_risk(ok=True)
    pipe = {"signals": {}, "allocation": {"notionals": {}}, "halted": False}
    loop = Loop(_FakeSettings(), lambda: pipe, risk, broker,
                lambda: _make_account(), db=db)
    res = loop.once()
    assert broker.sent == []
    assert db.saved == []
    assert res["action"] == "cycled"
