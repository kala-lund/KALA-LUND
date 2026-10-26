"""Fail-closed controls for legacy Alpaca paper-order submission."""

from __future__ import annotations

from typing import Any


PAPER_TRADING_ENDPOINT = "https://paper-api.alpaca.markets"
PAPER_BROKER_ENVIRONMENT = "paper"


class PaperExecutionSafetyError(PermissionError):
    """Raised when a paper-order precondition cannot be verified."""


def require_paper_endpoint(endpoint: str) -> str:
    """Require the exact approved Alpaca paper-trading endpoint."""
    if endpoint != PAPER_TRADING_ENDPOINT:
        raise ValueError("Only the exact Alpaca paper-trading endpoint is permitted.")
    return endpoint


def require_paper_configuration(
    api_key: str,
    api_secret: str,
    endpoint: str,
    broker_environment: str,
) -> str:
    """Validate static order configuration without exposing credential values."""
    approved_endpoint = require_paper_endpoint(endpoint)
    if broker_environment != PAPER_BROKER_ENVIRONMENT:
        raise PaperExecutionSafetyError("Broker environment could not be verified as paper.")
    if not isinstance(api_key, str) or not api_key.strip():
        raise PaperExecutionSafetyError("Alpaca API key is missing; order rejected.")
    if not isinstance(api_secret, str) or not api_secret.strip():
        raise PaperExecutionSafetyError("Alpaca API secret is missing; order rejected.")
    return approved_endpoint


def _account_status(account: Any) -> str:
    status = getattr(account, "status", None)
    value = getattr(status, "value", status)
    return value.strip().upper() if isinstance(value, str) else ""


def verify_paper_account(client: Any) -> Any:
    """Verify that the configured client can read an active paper account."""
    get_account = getattr(client, "get_account", None)
    if not callable(get_account):
        raise PaperExecutionSafetyError("Broker account status cannot be verified; order rejected.")
    try:
        account = get_account()
    except Exception as exc:  # noqa: BLE001 - convert broker failure to a safe denial
        raise PaperExecutionSafetyError("Broker account status could not be verified; order rejected.") from exc
    if _account_status(account) != "ACTIVE":
        raise PaperExecutionSafetyError("Broker account is not verified as ACTIVE; order rejected.")
    return account


def verify_client_paper_identity(client: Any, endpoint: str) -> None:
    """Reject clients whose own configured endpoint is live or unverifiable."""
    actual_endpoint = getattr(client, "_base_url", None)
    if actual_endpoint is None:
        actual_endpoint = getattr(client, "base_url", None)
    if actual_endpoint is None or str(actual_endpoint).rstrip("/") != endpoint:
        raise PaperExecutionSafetyError("Broker client endpoint could not be verified as paper.")
    sandbox = getattr(client, "_sandbox", None)
    if sandbox is False:
        raise PaperExecutionSafetyError("Broker client is configured for live trading; order rejected.")


def verify_paper_order_submission(
    client: Any,
    api_key: str,
    api_secret: str,
    endpoint: str,
    broker_environment: str,
) -> Any:
    """Run all static and account checks immediately before an order."""
    require_paper_configuration(api_key, api_secret, endpoint, broker_environment)
    verify_client_paper_identity(client, endpoint)
    return verify_paper_account(client)
