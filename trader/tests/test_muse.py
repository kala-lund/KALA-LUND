from __future__ import annotations

import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.ai import Muse, clean


def test_clean_schema_strict():
    good = {"action": "HOLD", "confidence": 0.6, "observations": "x",
            "evidence": ["e"], "abstain": [], "invalidate": "y"}
    assert clean(good) == {"action": "HOLD", "confidence": 0.6,
                           "observations": "x", "evidence": ["e"],
                           "abstain": [], "invalidate": "y"}
    for bad in ({"action": "BUY"}, {**good, "action": "MOON"},
                {**good, "confidence": 5}, "BUY", None, {}):
        assert clean(bad) is None


def test_muse_fail_closed_without_key():
    m = Muse(True, "", "claude-sonnet-4-5", "https://api.anthropic.com", 15, 1024)
    assert not m.available
    assert m.analyze({"s": 1}) is None


def test_muse_cooldown_on_failure(monkeypatch):
    import anthropic
    import trader.ai as aimod
    calls = {"n": 0}

    class FakeMsg:
        class B:
            type = "text"
            text = "not json"
        content = [B()]

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        def messages(self):
            return self
        def create(self, *a, **k):
            calls["n"] += 1
            raise RuntimeError("network down")

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    m = Muse(True, "key", "m", "http://x", 5, 1024)
    r1 = m.analyze({})
    assert r1 is None  # network failure → fail closed
    assert m._cool > time.time()  # cooldown engaged
    r2 = m.analyze({})
    assert r2 is None  # cooldown blocks second call
