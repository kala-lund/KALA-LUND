from __future__ import annotations

"""Structured logs (secrets redacted) + dashboard auth + input validation."""

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_ctx = threading.local()
_store: deque[dict[str, Any]] = deque(maxlen=2000)
_lock = threading.Lock()
_SECRET_HINTS = ("key", "secret", "token", "password", "auth")
_SYMBOL_RE = re.compile(r"^[A-Z]{2,10}/[A-Z]{2,10}$")

# Per-session error logging state (populated by init_session_logging).
_SESSION_ID: Optional[int] = None
_ERRORS_DIR: Optional[Path] = None
_SESSION_LOCK = threading.Lock()


def init_session_logging(base_dir: str | Path) -> int:
    """Initialize the per-session error logger and return the session ID.

    Safe to call repeatedly; returns the same session ID once initialized.
    """
    global _SESSION_ID, _ERRORS_DIR
    with _SESSION_LOCK:
        if _SESSION_ID is not None:
            return _SESSION_ID
        errors_dir = init_errors_dir(base_dir)
        _ERRORS_DIR = errors_dir
        _SESSION_ID = next_session_id()
        session_started(_SESSION_ID, errors_dir)
        return _SESSION_ID


def current_session_id() -> Optional[int]:
    return _SESSION_ID


def current_errors_dir() -> Optional[Path]:
    return _ERRORS_DIR


def shutdown_session_logging(had_errors: bool = True) -> None:
    """Finalize the current session file."""
    if _SESSION_ID is None or _ERRORS_DIR is None:
        return
    session_ended(_SESSION_ID, _ERRORS_DIR, had_errors=had_errors)



def set_cid(cid: str) -> None:
    _ctx.cid = cid


def cid() -> str:
    return getattr(_ctx, "cid", "-")


_SECRET_HINTS = ("key", "secret", "token", "password", "auth")
# value patterns that look like API keys / tokens / secrets
_SECRET_VALUE_PREFIXES = ("sk-", "pk_", "PKE", "sk_live", "sk_test", "rk_live", "rk_test")


def _looks_like_secret(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    if len(s) < 8:
        return False
    if any(s.startswith(p) for p in _SECRET_VALUE_PREFIXES):
        return True
    # long hex/base64-like token (>=32 chars alnum)
    if len(s) >= 32 and s.isalnum():
        return True
    return False


def _redact(o: Any) -> Any:
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            ks = str(k).lower()
            key_secret = any(h in ks for h in _SECRET_HINTS)
            if key_secret or _looks_like_secret(v):
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(o, (list, tuple)):
        return [_redact(v) for v in o]
    return o


def log(event: str, level: str = "info", **fields: Any) -> dict:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level,
           "event": event, "cid": cid(), **_redact(fields)}
    with _lock:
        _store.append(rec)
    logging.getLogger("trader").log({"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 20),
                                     json.dumps(rec, default=str))
    # Mirror error-level events to the per-session error log.
    if level in ("error", "critical") and _SESSION_ID is not None and _ERRORS_DIR is not None:
        exc = fields.pop("exc", None) if "exc" in fields else None
        try:
            write_session_entry(
                _SESSION_ID,
                _ERRORS_DIR,
                level=level.upper(),
                module=fields.get("module", ""),
                operation=fields.get("operation", ""),
                exc=exc,
                message=fields.get("message", event),
                request_id=fields.get("cid"),
            )
        except Exception:  # noqa: BLE001 - never crash the app over logging
            pass
    return rec


def recent(limit: int = 200, levels: tuple = ()) -> list[dict]:
    with _lock:
        items = list(_store)
    if levels:
        items = [r for r in items if r.get("level") in levels]
    return items[-limit:]


@dataclass
class Auth:
    ok: bool
    reason: str = ""


def check_ops(provided: str, configured: str) -> Auth:
    if not configured:
        return Auth(False, "Dashboard password not configured; operations locked.")
    if not provided:
        return Auth(False, "Missing password.")
    match = hmac.compare_digest(hashlib.sha256(provided.encode()).hexdigest(),
                                hashlib.sha256(configured.encode()).hexdigest())
    return Auth(match, "" if match else "Invalid password.")


def symbol(s: str) -> str:
    v = (s or "").strip().upper()
    if not _SYMBOL_RE.match(v):
        raise ValueError(f"Invalid symbol {s!r}; expected 'BASE/QUOTE' like BTC/USD.")
    return v


def qty(v: object) -> float:
    q = float(v)  # type: ignore[arg-type]
    if not (q > 0) or q in (float("inf"), float("-inf")):
        raise ValueError(f"Invalid quantity {v!r}.")
    return q


_DSN_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s:/@]+:[^\s@/]*@")
_SECRET_KV_RE = re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\s*[=:]\s*([^\s,;]+)")


def safe_msg(exc: BaseException) -> str:
    m = _DSN_CRED_RE.sub(r"\1[redacted]@", str(exc))
    m = _SECRET_KV_RE.sub(r"\1=[redacted]", m)
    if any(h in m.lower() for h in _SECRET_HINTS):
        return f"{type(exc).__name__}: [redacted]"
    return f"{type(exc).__name__}: {m[:300]}"


# Infrastructure detail that belongs in a server log but not in an HTTP response
# to an unauthenticated client: IP literals and explicit host name values.
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HOST_TOKEN_RE = re.compile(r"(?i)\b(?:host|hostaddr|hostname)\s*[=:]\s*[^\s,;'\"\)]+")


def public_text(msg: str) -> str:
    """Redact IP literals and ``host=...`` values from a message string."""
    m = _IPV4_RE.sub("[redacted-ip]", msg or "")
    return _HOST_TOKEN_RE.sub("host=[redacted]", m)


def public_error(exc: BaseException) -> str:
    """Sanitized error text safe to return to an unauthenticated API client.

    Builds on :func:`safe_msg` (which strips credentials, tokens and DSN password
    segments) and additionally removes host/IP topology so a diagnostics endpoint
    cannot be used to map the internal network.
    """
    return public_text(safe_msg(exc))


from .session_logger import (  # noqa: E402  (imported after safe_msg to avoid circular import)
    init_errors_dir,
    next_session_id,
    session_ended,
    session_started,
    write_session_entry,
)
