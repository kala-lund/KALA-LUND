"""Offline regression tests for every Alpaca order-submission boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trader.execution.safety import (
    PAPER_TRADING_ENDPOINT,
    PaperExecutionSafetyError,
    require_paper_configuration,
    require_paper_endpoint,
    verify_paper_order_submission,
)


ROOT = Path(__file__).parents[2]


def test_only_exact_paper_endpoint_is_accepted():
    assert require_paper_endpoint(PAPER_TRADING_ENDPOINT) == PAPER_TRADING_ENDPOINT
    for endpoint in (
        "https://api.alpaca.markets",
        "https://paper-api.alpaca.markets/v2",
        "https://paper-api.alpaca.markets.evil.example",
    ):
        with pytest.raises(ValueError):
            require_paper_endpoint(endpoint)


def test_missing_configuration_fails_closed_without_account_call():
    class Client:
        _base_url = PAPER_TRADING_ENDPOINT
        _sandbox = True

        def get_account(self):
            raise AssertionError("missing configuration must be rejected first")

    with pytest.raises(PaperExecutionSafetyError):
        verify_paper_order_submission(
            Client(), "", "secret", PAPER_TRADING_ENDPOINT, "paper"
        )
    with pytest.raises(PaperExecutionSafetyError):
        require_paper_configuration("key", "", PAPER_TRADING_ENDPOINT, "paper")


def test_account_status_must_be_verified_active():
    class Client:
        def __init__(self, status):
            self.status = status
            self._base_url = PAPER_TRADING_ENDPOINT
            self._sandbox = True

        def get_account(self):
            return type("Account", (), {"status": self.status})()

    with pytest.raises(PaperExecutionSafetyError):
        verify_paper_order_submission(
            Client("INACTIVE"), "key", "secret", PAPER_TRADING_ENDPOINT, "paper"
        )
    with pytest.raises(PaperExecutionSafetyError):
        verify_paper_order_submission(
            Client(None), "key", "secret", PAPER_TRADING_ENDPOINT, "paper"
        )
    assert verify_paper_order_submission(
        Client("ACTIVE"), "key", "secret", PAPER_TRADING_ENDPOINT, "paper"
    ).status == "ACTIVE"


def test_client_identity_is_required_and_live_clients_are_rejected():
    class Client:
        def get_account(self):
            return type("Account", (), {"status": "ACTIVE"})()

    with pytest.raises(PaperExecutionSafetyError):
        verify_paper_order_submission(
            Client(), "key", "secret", PAPER_TRADING_ENDPOINT, "paper"
        )

    class LiveClient(Client):
        _base_url = "https://api.alpaca.markets"
        _sandbox = False

    with pytest.raises(PaperExecutionSafetyError):
        verify_paper_order_submission(
            LiveClient(), "key", "secret", PAPER_TRADING_ENDPOINT, "paper"
        )


def _function_with_direct_submit_order(source: Path) -> ast.FunctionDef:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit_order"
            for node in ast.walk(function)
        ):
            return function
    raise AssertionError(f"No direct submit_order path found in {source}")


@pytest.mark.parametrize(
    "relative_path",
    (
        "trader/execution/broker.py",
        "backend/app/execution/alpaca_adapter.py",
        "finrl/meta/paper_trading/alpaca.py",
        "finrl/meta/env_stock_trading/env_stock_papertrading.py",
    ),
)
def test_every_direct_submit_order_path_has_immediate_safety_guard(relative_path):
    function = _function_with_direct_submit_order(ROOT / relative_path)
    submit_nodes = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit_order"
    ]
    names = {
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    calls = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "submit_order" in names
    assert "verify_paper_order_submission" in calls or "_verify_order_submission" in names
    guard_lines = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id == "verify_paper_order_submission")
             or (isinstance(node.func, ast.Attribute) and node.func.attr == "_verify_order_submission"))
    ]
    assert guard_lines and min(guard_lines) < min(node.lineno for node in submit_nodes)


@pytest.mark.parametrize(
    "relative_path",
    (
        "finrl/meta/paper_trading/alpaca.py",
        "finrl/meta/env_stock_trading/env_stock_papertrading.py",
    ),
)
def test_legacy_submission_requires_explicit_order_gate(relative_path):
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    submit = _function_with_direct_submit_order(ROOT / relative_path)
    assert "_legacy_order_approved" in source
    assert "if self._order_gate is None:" in source
    assert any(isinstance(node, ast.Attribute) and node.attr == "_verify_order_submission"
               for node in ast.walk(submit))
    gate = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "_legacy_order_approved")
    assert any(isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
               and node.value.value is False for node in ast.walk(gate))


def test_take_profit_uses_guarded_broker_boundary():
    loop = (ROOT / "trader/engine/loop.py").read_text(encoding="utf-8")
    broker = (ROOT / "trader/execution/broker.py").read_text(encoding="utf-8")
    assert 'reason="take_profit"' in loop
    assert "self.broker.send(sym, exit_side" in loop
    assert "verify_paper_order_submission" in broker
