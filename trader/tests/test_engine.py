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
