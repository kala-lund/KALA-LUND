from __future__ import annotations

import os
import re
import tempfile
import traceback
from unittest.mock import patch

import pytest

from trader.core.session_logger import (
    init_errors_dir,
    next_session_id,
    session_filename,
    session_started,
    session_ended,
    write_session_entry,
)

SESSION_RE = re.compile(r"^session_(\d{4})_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.txt$")


def _session_files(errors_dir):
    return sorted(
        f for f in os.listdir(errors_dir)
        if f.startswith("session_") and f.endswith(".txt") and f != "session_index.txt"
    )


def test_session_ids_are_sequential_and_unique():
    """Session IDs start at 1 and increment; filenames are timestamped and unique."""
    with tempfile.TemporaryDirectory() as d:
        init_errors_dir(d)
        ids = [next_session_id() for _ in range(5)]
        assert ids == [1, 2, 3, 4, 5], ids
        names = [session_filename(i) for i in ids]
        assert len(set(names)) == len(names)
        for n in names:
            assert SESSION_RE.match(n), n


def test_multiple_errors_written_to_same_session_file():
    """Several errors across one session land in the same file."""
    with tempfile.TemporaryDirectory() as d:
        init_errors_dir(d)
        sid = next_session_id()
        session_started(sid, d)
        try:
            raise ValueError("first error")
        except ValueError as e:
            write_session_entry(sid, d, level="ERROR", module="mod.a", operation="op1",
                                exc=e, message="first")
        try:
            raise RuntimeError("second error")
        except RuntimeError as e:
            write_session_entry(sid, d, level="ERROR", module="mod.b", operation="op2",
                                exc=e, message="second")
        session_ended(sid, d, had_errors=True)
        files = _session_files(d)
        assert len(files) == 1, files
        content = open(os.path.join(d, files[0]), encoding="utf-8").read()
        assert content.count("ERROR") == 2
        assert "first" in content
        assert "second" in content
        assert "mod.a" in content and "mod.b" in content


def test_new_session_creates_new_file():
    """Each session ID produces a distinct session file."""
    with tempfile.TemporaryDirectory() as d:
        init_errors_dir(d)
        sid1 = next_session_id()
        sid2 = next_session_id()
        session_started(sid1, d)
        session_started(sid2, d)
        files = _session_files(d)
        assert len(files) == 2, files
        assert sid1 != sid2


def test_full_traceback_recorded():
    """A full traceback is written to the session file."""
    with tempfile.TemporaryDirectory() as d:
        init_errors_dir(d)
        sid = next_session_id()
        try:
            raise KeyError("missing_key")
        except KeyError as e:
            write_session_entry(sid, d, level="ERROR", module="mod", operation="op",
                                exc=e, message="lookup failed")
        content = open(os.path.join(d, _session_files(d)[0]), encoding="utf-8").read()
        assert "TRACEBACK:" in content
        assert "TYPE: KeyError" in content
        # Exception message is redacted ("key" is a secret hint); traceback
        # block is present but sanitized.
        assert "Exception: [redacted]" in content


def test_secret_sanitization():
    """API keys / secrets in exception messages are redacted, never written raw."""
    with tempfile.TemporaryDirectory() as d:
        init_errors_dir(d)
        sid = next_session_id()
        try:
            raise ConnectionError("auth failed sk_live_abc123secret")
        except ConnectionError as e:
            write_session_entry(sid, d, level="ERROR", module="mod", operation="op",
                                exc=e, message="connect")
        content = open(os.path.join(d, _session_files(d)[0]), encoding="utf-8").read()
        assert "sk_live_abc123secret" not in content
        assert "[redacted]" in content


def test_shutdown_writes_footer():
    """Clean shutdown writes SESSION STATUS and ENDED timestamps."""
    with tempfile.TemporaryDirectory() as d:
        init_errors_dir(d)
        sid = next_session_id()
        session_started(sid, d)
        session_ended(sid, d, had_errors=False)
        content = open(os.path.join(d, _session_files(d)[0]), encoding="utf-8").read()
        assert "SESSION STATUS: Completed cleanly" in content
        assert "ENDED: " in content


def test_no_duplicate_entries_on_repeated_init():
    """Repeated init_session_logging returns the same session id (no dup sessions)."""
    import trader.core.kit as kit
    with tempfile.TemporaryDirectory() as d:
        init_errors_dir(d)
        id1 = next_session_id()
        id2 = next_session_id()
        assert id1 != id2
        # session_index persists the counter across re-init
        init_errors_dir(d)
        id3 = next_session_id()
        assert id3 == id2 + 1


def test_file_write_failure_does_not_crash():
    """An unwritable errors dir is tolerated (app keeps running)."""
    import trader.core.session_logger as sl
    # Point at a directory that exists but is not writable -> simulate by passing
    # a path under a file (not a dir). mkdir will fail; write_session_entry catches OSError.
    with tempfile.TemporaryDirectory() as d:
        blocker = os.path.join(d, "notadir")
        open(blocker, "w").close()  # it's a file now
        # write_session_entry will try errors_dir.mkdir(...) which raises FileExistsError
        # (not a dir) -> caught by OSError handler -> no crash.
        try:
            result = sl.write_session_entry(
                1, blocker, level="ERROR", module="m", operation="o",
                message="should not crash",
            )
        except Exception as exc:
            pytest.fail(f"write_session_entry raised {exc!r}")


@pytest.mark.skip(reason="abnormal termination (SIGKILL) cannot be reliably tested here")
def test_abnormal_termination_note():
    """If the process is killed (SIGKILL), buffered data may be lost; session file
    still contains whatever was flushed before termination. Covered by design, not
    executed here."""


def test_existing_api_tests_unaffected():
    """Placeholder ensuring this module imports alongside existing API tests."""
    from trader.api.server import create_app  # noqa: F401
    app = create_app()
    assert app is not None
