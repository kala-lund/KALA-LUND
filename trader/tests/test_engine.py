from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from types import SimpleNamespace

from trader.ai import clean
from trader.data.market import MarketStore
from trader.engine.loop import Loop, run_cycle
from trader.execution.broker import Broker, canon as _unused  # noqa: F401 (re-export check)
from trader.execution.broker import Broker as _B
from trader.data.market import canon
from trader.risk.guard import Account, Limits, Risk


def test_ai_schema_and_closed():
    good = {"action": "HOLD", "confidence": 0.6, "observations": "x",
            "evidence": ["e"], "abstain": [], "invalidate": "y"}
    assert clean(good) is not None
    for bad in ({"action": "BUY"}, {**good, "action": "MOON"},
                {**good, "confidence": 5}, "BUY", None):
        assert clean(bad) is None
    from trader.ai import Muse
    m = Muse(True, "", "m", "http://x", 5, 10)
    assert not m.available and m.analyze({}) is None


def test_broker_guards_and_canon():
    assert canon("BTCUSD") == "BTC/USD"
    try:
        Broker("k", "s", "https://api.alpaca.markets", False)
        assert False
    except ValueError:
        pass
    b = Broker("k", "s", "https://paper-api.alpaca.markets", False)
    try:
        b.send("BTC/USD", "BUY", notional=10)
        assert False
    except PermissionError:
        pass


class FakeBroker:
    def __init__(self):
        self.sent = []

    def send(self, sym, side, **kw):
        self.sent.append((sym, side))
        return SimpleNamespace(status="filled")

    def get_open_orders(self):
        return [], None


def _s(**kw):
    d = {"crypto_symbols": ("BTC/USD",), "automate_dry_run": True, "automate_minutes": 15,
         "automate_min_conf": 0.5, "automate_max_notional": 100.0, "enable_paper_orders": False,
         "max_position_notional": 2000.0}
    d.update(kw)
    return SimpleNamespace(**d)


def _pipe(act="BUY", conf=0.9):
    return {"halted": False,
            "signals": {"BTC/USD": {"aggregate": {"action": act, "confidence": conf, "score": 0.8}}},
            "allocation": {"notionals": {"BTC/USD": 80.0}}}


def _acct(pos=None):
    return Account(10000.0, 10000.0, pos or {}, 0, 0.0, 10000.0, 5.0)


def test_loop_safety():
    fb = FakeBroker()
    lo = Loop(_s(), lambda: _pipe(), Risk(Limits()), fb, lambda: _acct())
    assert lo.once()["action"] == "cycled" and fb.sent == [] and lo.st.dry_orders == 1
    lo2 = Loop(_s(), lambda: _pipe(act="ABSTAIN", conf=0.1), Risk(Limits()), fb, lambda: _acct())
    assert fb.sent == []
    lo3 = Loop(_s(), lambda: {"halted": True, "halt_reason": "x"}, Risk(Limits()), fb, lambda: _acct())
    assert lo3.once()["action"] == "halted"
    g = Risk(Limits())
    g.trip("t")
    assert Loop(_s(), lambda: _pipe(), g, fb, lambda: _acct()).once()["action"] == "halted"
    held = lambda: _acct({"BTC/USD": 600.0})  # noqa: E731
    lo5 = Loop(_s(automate_dry_run=False, enable_paper_orders=True), lambda: _pipe(), Risk(Limits()), fb, held)
    lo5.once()
    assert fb.sent == []  # already positioned -> skipped
    assert lo5.start().running and not lo5.stop().running


def test_concurrent_store():
    from trader.data.market import Cache

    class P:
        def fetch(self, s, tf, a, b):
            return [{"ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]

    ms = MarketStore(P(), Cache(10, 60), workers=4)  # type: ignore[arg-type]
    r = ms.many(["A/USD", "B/USD"], "15Min", limit=5)
    assert len(r) == 2 and all(isinstance(v, list) for v in r.values())


# ---------------------------------------------------------------------------
# Durable-store gate for automated execution.
# ---------------------------------------------------------------------------
class _DurableStore:
    def __init__(self, ok=True, fail_write=False):
        self.ok, self.fail_write, self.saved = ok, fail_write, []

    def durable_available(self):
        return {"ok": self.ok, "backend": "postgres",
                "detail": "postgres" if self.ok else "connection refused"}

    def save_order(self, *args):
        if self.fail_write:
            raise RuntimeError("durable store write failed")
        self.saved.append(args)


def test_loop_halts_before_submitting_without_durable_store():
    """No order may reach the broker when durable persistence is unavailable."""
    fb = FakeBroker()
    store = _DurableStore(ok=False)
    lo = Loop(_s(automate_dry_run=False, enable_paper_orders=True,
                 require_durable_store=True),
              lambda: _pipe(), Risk(Limits()), fb, lambda: _acct(), db=store)
    out = lo.once()
    assert out["action"] == "halted"
    assert "durable" in out["reason"].lower()
    assert fb.sent == []
    assert store.saved == []


def test_loop_trips_kill_switch_when_a_submitted_order_cannot_be_recorded():
    from trader.execution.broker import Fill

    class _B(FakeBroker):
        def send(self, sym, side, **kw):
            self.sent.append((sym, side))
            return Fill(cid="c1", status="filled", symbol=sym, side=side,
                        qty=None, filled_qty=1.0, filled_price=100.0)

    store = _DurableStore(ok=True, fail_write=True)
    risk = Risk(Limits())
    lo = Loop(_s(automate_dry_run=False, enable_paper_orders=True,
                 require_durable_store=True),
              lambda: _pipe(), risk, _B(), lambda: _acct(), db=store)
    lo.once()
    assert risk.killed is True
