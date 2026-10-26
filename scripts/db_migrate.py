#!/usr/bin/env python
"""Apply and verify the durable PostgreSQL schema (Neon/Supabase compatible).

This is the repeatable migration gate for production:

    # 1. Put the Neon pooled DSN in DATABASE_URL (env or repo .env)
    # 2. Apply the idempotent schema + verify tables, indexes and counts
    python scripts/db_migrate.py

Exit codes:
    0  schema applied and verified
    1  verification failed (tables/indexes/version/counts)
    2  configuration or connection failed

Safety:
    * Only additive DDL (``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF
      NOT EXISTS``) and an idempotent schema-version upsert. No DROP/DELETE.
    * Never prints the DSN, host, user or password.
    * Never reads or writes another database, so a source database (Supabase)
      is never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from trader.core.config import load_settings, validate_settings  # noqa: E402
from trader.core.kit import safe_msg  # noqa: E402
from trader.data.db_store import DBStore, is_local, is_neon  # noqa: E402


def _target_label(url: str) -> str:
    if is_local(url):
        return "local"
    if is_neon(url):
        return "neon-pooled" if "-pooler" in url.lower() else "neon-direct"
    if "supabase" in url.lower():
        return "supabase"
    return "remote"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    s = load_settings()
    errors = [e for e in validate_settings(s) if "DATABASE_URL" in e or "postgres" in e]
    if not s.database_url:
        print("db_migrate: DATABASE_URL is not configured.", file=sys.stderr)
        return 2
    if errors:
        for e in errors:
            print(f"db_migrate: {e}", file=sys.stderr)
        return 2

    try:
        # require_durable=True makes a connection failure fatal instead of
        # silently falling back to in-memory storage.
        db = DBStore(
            s.database_url,
            connect_timeout=s.database_connect_timeout,
            require_durable=True,
            connect_retries=s.database_connect_retries,
            sslmode=s.database_sslmode,
            application_name=s.database_application_name,
        )
    except Exception as exc:  # noqa: BLE001 - sanitized, never includes the DSN
        print(f"db_migrate: connection failed: {safe_msg(exc)}", file=sys.stderr)
        return 2

    try:
        report = db.verify()
        health = db.health_check()
        target = _target_label(s.database_url)
    finally:
        db.close()

    result = {
        "target": target,
        "health": health,
        "schema": report,
        "ok": bool(report.get("ok")) and health.get("status") == "ok",
    }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"db_migrate: target={target} backend=postgresql")
        print(f"db_migrate: health={health.get('status')} latency_ms={health.get('latency_ms')}")
        print(f"db_migrate: schema_ok={report.get('ok')} version={report.get('schema_version')} "
              f"expected={report.get('expected_version')}")
        print(f"db_migrate: tables={report.get('tables')}")
        if report.get("missing_tables"):
            print(f"db_migrate: MISSING TABLES {report['missing_tables']}", file=sys.stderr)
        if report.get("missing_columns"):
            print(f"db_migrate: MISSING COLUMNS {report['missing_columns']}", file=sys.stderr)
        if report.get("missing_indexes"):
            print(f"db_migrate: MISSING INDEXES {report['missing_indexes']}", file=sys.stderr)
        print(f"db_migrate: counts={report.get('counts')}")
        if report.get("error"):
            print(f"db_migrate: error={report['error']}", file=sys.stderr)
        if target == "neon-direct":
            print("db_migrate: note - this is the unpooled Neon endpoint. Use the "
                  "pooled (-pooler) DSN for the long-running application.")
        if target == "neon-pooled":
            print("db_migrate: note - pooled endpoints do not support every session "
                  "feature; migrations here are simple additive DDL and are safe.")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
