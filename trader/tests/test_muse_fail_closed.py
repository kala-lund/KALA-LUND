from __future__ import annotations

"""Muse AI fail-closed behavior tests.

Covers: Muse disabled (no key / flag false), unavailable, invalid JSON responses,
schema-rejected responses, timeouts, cooldown, and explicit fallback behavior
when analyze() returns None.
"""

import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from trader.ai import Muse, clean


class FakeAnthropic:
    """Mock Anthropic client returning controlled responses or raising."""
    def __init__(self, *a, **k):
        self.mode = "ok"  # ok | timeout | invalid_json | schema_invalid | schema_reject

    class _Msg:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class _Content:
        def __init__(self, text):
            self.content = [FakeAnthropic._Msg(text)]

    class _Messages:
        def __init__(self, outer):
            self.outer = outer
        def create(self, *a, **k):
            if self.outer.mode == "timeout":
                raise TimeoutError("request timed out")
            if self.outer.mode == "invalid_json":
                return FakeAnthropic._Content("{ not valid json")
            if self.outer.mode == "schema_invalid":
                return FakeAnthropic._Content(
                    '{"action": "MOON", "confidence": 0.5, "observations": "x", '
                    '"evidence": [], "abstain": [], "invalidate": "y"}')
            if self.outer.mode == "schema_reject":
                return FakeAnthropic._Content('{"action": "BUY", "confidence": 0.5}')
            return FakeAnthropic._Content(
                '{"action": "HOLD", "confidence": 0.7, "observations": "flat", '
                '"evidence": [], "abstain": [], "invalidate": "none"}')

    @property
    def messages(self):
        return FakeAnthropic._Messages(self)


def test_muse_disabled_no_key():
    m = Muse(True, "", "claude-sonnet-4-5", "https://api.anthropic.com", 15, 1024)
    assert not m.available
    assert m.analyze({}) is None


def test_muse_disabled_flag_false():
    m = Muse(False, "key", "m", "http://x", 5, 1024)
    assert not m.available
    assert m.analyze({}) is None


def test_muse_invalid_json_response(monkeypatch):
    fake = FakeAnthropic()
    fake.mode = "invalid_json"
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: fake)
    m = Muse(True, "key", "m", "http://x", 5, 1024)
    assert m.analyze({}) is None  # invalid JSON -> fail closed
    assert m.err  # error recorded


def test_muse_schema_reject_response(monkeypatch):
    fake = FakeAnthropic()
    fake.mode = "schema_reject"
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: fake)
    m = Muse(True, "key", "m", "http://x", 5, 1024)
    assert m.analyze({}) is None  # schema reject -> fail closed


def test_muse_schema_invalid_action(monkeypatch):
    fake = FakeAnthropic()
    fake.mode = "schema_invalid"
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: fake)
    m = Muse(True, "key", "m", "http://x", 5, 1024)
    assert m.analyze({}) is None  # invalid action -> fail closed


def test_muse_timeout(monkeypatch):
    fake = FakeAnthropic()
    fake.mode = "timeout"
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: fake)
    m = Muse(True, "key", "m", "http://x", 5, 1024)
    assert m.analyze({}) is None  # timeout -> fail closed
    assert m._cool > time.time()  # cooldown engaged
    # second call during cooldown also returns None without hitting network
    fake.mode = "ok"
    assert m.analyze({}) is None


def test_muse_valid_response_returns_view(monkeypatch):
    fake = FakeAnthropic()
    fake.mode = "ok"
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: fake)
    m = Muse(True, "key", "claude-sonnet-4-5", "https://api.anthropic.com", 15, 1024)
    v = m.analyze({})
    assert v is not None
    assert v.action == "HOLD"
    assert v.confidence == 0.7
    assert v.model == "claude-sonnet-4-5"


def test_clean_rejects_malformed():
    assert clean(None) is None
    assert clean("BUY") is None
    assert clean({}) is None
    assert clean({"action": "BUY"}) is None  # missing keys
    good = {"action": "HOLD", "confidence": 0.6, "observations": "x",
            "evidence": ["e"], "abstain": [], "invalidate": "y"}
    assert clean(good) is not None
    assert clean({"action": "MOON", "confidence": 0.5, "observations": "x",
                  "evidence": [], "abstain": [], "invalidate": "y"}) is None
    assert clean({**good, "confidence": 1.5}) is None  # out of range
