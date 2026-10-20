from __future__ import annotations

"""Per-session error logging for FinRL applications.

Creates one plain-text log file per application session under ``errors/``:

    session_0001_YYYY-MM-DD_HH-MM-SS.txt

Session IDs are allocated sequentially from a counter file
``errors/session_index.txt``.  The counter update is best-effort atomic:
each process claims the next integer by reading, incrementing, and writing
the file under a lock.  Under genuine simultaneous process starts there is a
small race window (two processes read the same value before either writes);
to keep this dependency-free we document single-process usage as the
supported mode and recommend external coordination (file lock / orchestrator)
for multi-process deployment.  Historical files are never deleted or
overwritten; restarts append new session files.

No API keys, passwords, tokens, connection URLs containing credentials, or
auth headers are ever written.  Exception messages and tracebacks are passed
through :func:`trader.core.kit.safe_msg` before being persisted.
"""

import os
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ERRORS_DIR_NAME = "errors"
SESSION_INDEX_NAME = "session_index.txt"
SESSION_PREFIX = "session_"
SESSION_ID_WIDTH = 4

_lock = threading.Lock()
_index_path: Optional[Path] = None
_errors_dir: Optional[Path] = None


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_file_str() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def init_errors_dir(base_dir: str | Path) -> Path:
    """Return (and create if missing) the errors directory.

    Safe to call repeatedly; returns the same Path.
    """
    global _errors_dir, _index_path
    base = Path(base_dir)
    errors_dir = base / ERRORS_DIR_NAME
    errors_dir.mkdir(parents=True, exist_ok=True)
    _errors_dir = errors_dir
    _index_path = errors_dir / SESSION_INDEX_NAME
    return errors_dir


def _read_index() -> int:
    if _index_path is None:
        raise RuntimeError("errors dir not initialized")
    try:
        text = _index_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _write_index(value: int) -> None:
    if _index_path is None:
        raise RuntimeError("errors dir not initialized")
    tmp = _index_path.with_suffix(".tmp")
    tmp.write_text(f"{value}\n", encoding="utf-8")
    tmp.replace(_index_path)


def next_session_id() -> int:
    """Atomically claim and return the next session ID (starting at 1)."""
    with _lock:
        current = _read_index()
        nxt = current + 1
        _write_index(nxt)
        return nxt


def session_filename(session_id: int) -> str:
    return f"{SESSION_PREFIX}{session_id:0{SESSION_ID_WIDTH}d}_{_now_file_str()}.txt"


# One file per (session id, errors dir). The timestamp is captured once, when the
# session file is first created, so a session can never be split across two files
# just because a write happened in the next second.
_file_cache: dict[tuple[int, str], str] = {}


def _session_file(session_id: int, errors_dir: Path) -> str:
    """Return the stable file name for a session inside ``errors_dir``."""
    key = (session_id, str(errors_dir))
    name = _file_cache.get(key)
    if name is None:
        name = session_filename(session_id)
        _file_cache[key] = name
    return name


def _sanitize_traceback(exc: Optional[BaseException]) -> str:
    if exc is None:
        return ""
    from .kit import safe_msg  # lazy to avoid circular import at module load

    if not isinstance(exc, BaseException):
        # trader.core.kit.log() forwards an already-sanitized *string* (safe_msg).
        # Accessing .__traceback__ on it raised AttributeError, which was
        # swallowed upstream and silently dropped error entries from the session
        # audit log. Treat non-exception input as message text and never lose it.
        return safe_msg(Exception(str(exc)))
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    tb = "".join(lines)
    tb = safe_msg(Exception(tb))  # redact secret-like substrings
    return tb


def write_session_entry(
    session_id: int,
    errors_dir: str | Path,
    *,
    level: str = "ERROR",
    module: str = "",
    operation: str = "",
    exc: Optional[BaseException] = None,
    message: str = "",
    request_id: Optional[str] = None,
) -> Path:
    """Append one entry to the session file, creating it if needed.

    The header (SESSION ID / STARTED / ...) is written once when the file is
    first created.  Subsequent errors append [timestamp] blocks.  Returns the
    session file path.
    """
    errors_dir = Path(errors_dir)
    fname = _session_file(session_id, errors_dir)
    fpath = errors_dir / fname
    is_new = not fpath.exists()

    now = _now_str()
    blocks: list[str] = []

    if is_new:
        blocks.append("SESSION ID: " f"{session_id:0{SESSION_ID_WIDTH}d}")
        blocks.append(f"STARTED: {now}")
        blocks.append("APPLICATION: FinRL")
        blocks.append("ENVIRONMENT: Paper trading")
        blocks.append("")

    blocks.append(f"[{now}] {level}")
    if module:
        blocks.append(f"MODULE: {module}")
    if operation:
        blocks.append(f"OPERATION: {operation}")
    if request_id:
        blocks.append(f"REQUEST_ID: {request_id}")
    tb = _sanitize_traceback(exc) if exc else ""
    if tb:
        from .kit import safe_msg  # lazy to avoid circular import at module load
        blocks.append(f"TYPE: {type(exc).__name__ if exc else level}")
        blocks.append(f"MESSAGE: {message or safe_msg(exc)}")
        blocks.append("TRACEBACK:")
        blocks.append(tb)
    else:
        blocks.append(f"MESSAGE: {message}")
    blocks.append("")

    text = "\n".join(blocks) + "\n"
    try:
        errors_dir.mkdir(parents=True, exist_ok=True)
        with fpath.open("a", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        # Never crash the application because of a logging failure.
        pass
    return fpath


def session_started(session_id: int, errors_dir: str | Path) -> Path:
    """Create/initialize a session file and record the start."""
    return write_session_entry(
        session_id,
        errors_dir,
        level="INFO",
        module="session",
        operation="start",
        message="Application session started",
    )


def session_ended(
    session_id: int,
    errors_dir: str | Path,
    *,
    had_errors: bool = True,
) -> Path:
    """Finalize a session file with status and end timestamp."""
    now = _now_str()
    status = "Completed with errors" if had_errors else "Completed cleanly"
    return _append_footer(session_id, errors_dir, status, now)


def _append_footer(
    session_id: int,
    errors_dir: str | Path,
    status: str,
    now: str,
) -> Path:
    errors_dir = Path(errors_dir)
    fname = _session_file(session_id, errors_dir)
    fpath = errors_dir / fname
    footer = f"\nSESSION STATUS: {status}\nENDED: {now}\n"
    try:
        if fpath.exists():
            with fpath.open("a", encoding="utf-8") as fh:
                fh.write(footer)
                fh.flush()
                os.fsync(fh.fileno())
    except OSError:
        pass
    return fpath


__all__ = [
    "init_errors_dir",
    "next_session_id",
    "session_filename",
    "write_session_entry",
    "session_started",
    "session_ended",
]
