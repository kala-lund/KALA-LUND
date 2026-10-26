from __future__ import annotations

import math
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.execution.broker import Broker, PAPER_HOST, Fill, _install_request_timeout


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


# ---------------------------------------------------------------------------
# Read-path robustness: a hung broker must not make the account permanently
# unreadable by starving the read pool (regression for
# "Broker read 'account' exceeded 10.0s" persisting after the network recovers).
# ---------------------------------------------------------------------------
def test_read_timeout_releases_the_pool_for_later_reads():
    calls = {"n": 0}

    class SlowThenFastClient:
        _base_url = "https://paper-api.alpaca.markets"
        _sandbox = True

        def get_account(self):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(1.5)  # simulates a half-open socket
            return type("A", (), {
                "equity": "100", "buying_power": "10", "cash": "10",
                "last_equity": "100", "status": "ACTIVE",
                "long_market_value": "0"})()

    b = Broker("k", "s", "https://paper-api.alpaca.markets", False,
               client=SlowThenFastClient(), read_timeout=0.1)
    t0 = time.perf_counter()
    try:
        b.account()
        assert False, "a hung read must time out"
    except TimeoutError:
        pass
    first = time.perf_counter() - t0
    acct = b.account()  # must not be queued behind the abandoned worker
    second = time.perf_counter() - t0 - first
    assert acct["status"] == "ACTIVE"
    assert first < 1.0, first
    assert second < 1.0, second
    b.close()


def test_hung_socket_is_bounded_by_the_installed_timeout():
    """The injected per-request timeout must fire on a stalled peer."""
    import socket
    import threading

    import requests

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def accept_and_stall():
        try:
            conn, _ = srv.accept()
            time.sleep(2.0)  # accept, never answer
            conn.close()
        except Exception:  # noqa: BLE001 - test cleanup
            pass

    threading.Thread(target=accept_and_stall, daemon=True).start()

    class Client:
        _session = requests.Session()

    _install_request_timeout(Client, 0.3)
    t0 = time.perf_counter()
    try:
        Client._session.request("GET", f"http://127.0.0.1:{port}/v2/account")
        assert False, "a stalled socket must raise"
    except requests.exceptions.RequestException:
        pass
    assert time.perf_counter() - t0 < 1.5
    srv.close()


# ---------------------------------------------------------------------------
# Bounded, read-only account probe (diagnostics only; never submits an order).
# ---------------------------------------------------------------------------
PAPER = "https://paper-api.alpaca.markets"
KEY, SECRET = "PK" + "A" * 24, "SEC" + "b" * 40


def _raising(exc):
    def _read(label, fn):
        raise exc
    return _read


def test_probe_missing_credentials_makes_no_call():
    b = Broker("", "", PAPER, False)
    missing = b.probe()
    assert missing["available"] is False
    assert missing["status"] == "missing_credentials"
    assert missing["paper"] is True
    assert missing["endpoint"] == PAPER
    assert missing["latency_ms"] is None
    assert "credential" in missing["detail"].lower()


def test_probe_rejected_credentials_are_classified(monkeypatch):
    class _Resp:
        status_code = 401

    class _Err(Exception):
        response = _Resp()

    b = Broker(KEY, SECRET, PAPER, False)
    monkeypatch.setattr(b, "_read", _raising(_Err("HTTP 401 Unauthorized")))
    out = b.probe()
    assert out["available"] is False
    assert out["status"] == "auth_failed"
    assert "401" in out["detail"]


def test_probe_timeout_is_classified(monkeypatch):
    b = Broker(KEY, SECRET, PAPER, False)
    monkeypatch.setattr(b, "_read", _raising(
        TimeoutError("Broker read 'probe_account' exceeded 10.0s")))
    out = b.probe()
    assert out["status"] == "timeout"
    assert out["available"] is False
    assert out["latency_ms"] is not None


def test_probe_network_failure_is_classified(monkeypatch):
    b = Broker(KEY, SECRET, PAPER, False)
    monkeypatch.setattr(b, "_read", _raising(ConnectionError("Connection refused")))
    out = b.probe()
    assert out["status"] == "network_error"
    assert out["available"] is False


def test_probe_success_is_read_only_and_secret_free():
    class _Account:
        status = type("S", (), {"value": "ACTIVE"})()

    class _Client:
        _base_url = PAPER
        _sandbox = True

        def __init__(self):
            self.calls = []

        def get_account(self):
            self.calls.append("get_account")
            return _Account()

    client = _Client()
    b = Broker(KEY, SECRET, PAPER, True, client=client)
    out = b.probe()
    assert out["available"] is True and out["status"] == "ok"
    assert out["account_status"] == "ACTIVE"
    assert out["paper"] is True
    # Exactly one read; nothing that could change order state exists on the fake.
    assert client.calls == ["get_account"]
    blob = str(out)
    assert KEY not in blob and SECRET not in blob


def test_send_aborts_without_submitting_when_account_read_times_out():
    """A broker timeout during verification must fail closed."""
    import requests

    class TimingOutClient:
        _base_url = "https://paper-api.alpaca.markets"
        _sandbox = True
        submitted = False

        def get_account(self):
            raise requests.exceptions.ReadTimeout("read timed out")

        def submit_order(self, req):  # pragma: no cover - must not be reached
            self.submitted = True

    client = TimingOutClient()
    b = Broker("k", "s", "https://paper-api.alpaca.markets", True, client=client)
    try:
        b.send("BTC/USD", "BUY", notional=10)
        assert False, "a timed-out verification must raise"
    except Exception:
        pass
    assert not client.submitted
    b.close()


def test_client_is_memoized_per_thread(monkeypatch):
    import alpaca.trading.client as trading

    created = []

    class FakeClient:
        _base_url = "https://paper-api.alpaca.markets"
        _sandbox = True

        def __init__(self, *a, **k):
            created.append(1)

    monkeypatch.setattr(trading, "TradingClient", FakeClient)
    b = Broker("k", "s", "https://paper-api.alpaca.markets", False)
    first = b._c()
    assert b._c() is first  # reuses the client (and its TLS connection)
    assert len(created) == 1
    b.close()


def test_request_timeout_is_installed_once():
    class Session:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append(kwargs)
            return "ok"

    class Client:
        _session = Session()

    _install_request_timeout(Client, 7.0)
    _install_request_timeout(Client, 7.0)  # idempotent, never double-wraps
    assert Client._session.request("GET", "https://paper-api.alpaca.markets/v2/account") == "ok"
    assert Client._session.request("POST", "https://paper-api.alpaca.markets/v2/orders") == "ok"
    assert len(Client._session.calls) == 2
    # Reads are tight; submissions get a longer (still bounded) window so a
    # slow submit is not abandoned into ambiguous order state prematurely.
    assert Client._session.calls[0]["timeout"] == (5.0, 7.0)
    assert Client._session.calls[1]["timeout"] == (5.0, 30.0)
