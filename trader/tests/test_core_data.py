from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.core.config import FULL_USD_UNIVERSE, load_settings, validate_settings
from trader.core.kit import check_ops, qty as v_qty, safe_msg, symbol as v_sym
from trader.data.market import Cache, MarketStore, canon
from trader.data.quality import check as qcheck
from trader.features.feat import features


def bars(n=90, flat=False):
    out, p = [], 100.0
    for i in range(n):
        if not flat:
            p = p * 1.001 if i % 2 == 0 else p * 0.9995
        out.append({"ts": 1_700_000_000_000 + i * 900_000, "o": p * 0.999, "h": p * 1.001,
                    "l": p * 0.999, "c": p, "v": 0.0 if flat and i > 10 else 10.0 + i % 5})
    return out


def test_config_aliases_and_guards(monkeypatch):
    for n in ("CRYPTO_SYMBOLS", "ALPACA_API_KEY", "APCA_API_KEY", "APCA_PAPER1_API_KEY",
              "ALPACA_API_SECRET", "APCA_API_SECRET", "ALPACA_PAPER_BASE_URL", "APCA_BASE_URL",
              "ENABLE_LIVE_TRADING", "TRADING_MODE"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("APCA_API_KEY", "k")
    monkeypatch.setenv("APCA_API_SECRET", "s")
    monkeypatch.setenv("APCA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    s = load_settings()
    assert (s.alpaca_api_key, s.alpaca_api_secret) == ("k", "s")
    assert s.alpaca_paper_base_url == "https://paper-api.alpaca.markets"
    assert validate_settings(s) == []
    monkeypatch.setenv("CRYPTO_SYMBOLS", "ALL")
    s2 = load_settings()
    assert len(s2.crypto_symbols) == len(FULL_USD_UNIVERSE) >= 30
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    assert any("LIVE" in e for e in validate_settings(load_settings()))
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setenv("APCA_PAPER1_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    assert any("endpoint" in e.lower() for e in validate_settings(load_settings()))


def test_config_rejects_cors_wildcard(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "*")
    errors = validate_settings(load_settings())
    assert any("CORS_ORIGINS" in e and "*" in e for e in errors)


def test_canon_and_validation():
    assert canon("BTCUSD") == "BTC/USD" and canon("ETH/BTC") == "ETH/BTC"
    assert v_sym("btc/usd") == "BTC/USD"
    assert v_qty(5) == 5.0
    try:
        v_sym("nope")
        assert False
    except ValueError:
        pass
    assert check_ops("", "").ok is False
    assert check_ops("pw", "pw").ok is True
    assert "redacted" in safe_msg(ValueError("my api key leak"))


def test_features_and_quality():
    f1, f2 = features(bars()), features(bars())
    assert f1 == f2 and f1[0]["s20"] is None and f1[-1]["s20"] is not None
    r = qcheck(bars(), 900_000, 10 ** 9)
    assert r["ok"] and r["gaps"] == 0
    assert qcheck(bars(), 900_000, 0)["stale"] is True
    # degenerate: zero-volume / flat / zero-price must not raise
    for b in (bars(flat=True),
              [{"ts": i, "o": 5, "h": 5, "l": 5, "c": 5, "v": 1} for i in range(90)],
              [{"ts": i, "o": 0, "h": 0, "l": 0, "c": 0, "v": 1} for i in range(90)]):
        features(b)


def test_cache_bounded():
    c = Cache(3, 60)
    for i in range(5):
        c.put(str(i), i)
    assert c.stats()["entries"] <= 3
