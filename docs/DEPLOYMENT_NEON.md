# Deployment: Neon PostgreSQL (durable store)

**Status (2026-10-26):** schema applied and verified against the Neon
`production` branch; `psycopg2` connectivity confirmed; no application data has
been copied from the legacy Supabase project (deliberate: the Neon store starts
empty and is schema-only).

**Scope:** the FastAPI backend (`trader/`) uses Neon as its durable source of
truth for signals, backtests and order records. Redis remains an optional cache.
Paper trading only; live trading is prohibited.

---

## 1. Why Neon

- The previous Supabase **direct** host was IPv6-only and unreachable from
  Railway. Neon's pooled endpoint publishes both A and AAAA records.
- Standard PostgreSQL wire protocol: `psycopg2` and the existing `DBStore`
  interface are unchanged.
- Pooled connections reduce the number of server-side backends for a
  long-running, low-traffic service.

Neon is **not** a drop-in data migration: the schema is recreated additively and
the legacy Supabase database is left untouched.

## 2. Connection settings

Use the **pooled** DSN (Neon Console -> Connect -> *Connection pooling*):

```
postgresql://<user>:<password>@ep-<id>-pooler.<region>.aws.neon.tech/<db>?sslmode=require
```

| Setting | Value | Where |
|---|---|---|
| `DATABASE_URL` | pooled DSN | Railway service variables / local `.env` |
| `DATABASE_URL_UNPOOLED` | direct DSN | migrations only; never used by the API |
| `DB_SSLMODE` | `require` | auto-applied for non-local hosts when the DSN has no `sslmode` |
| `DB_CONNECT_TIMEOUT` | `5` (seconds) | bounds a hung TCP/TLS handshake |
| `DB_CONNECT_RETRIES` | `3` | bounded retries with backoff |
| `DB_APPLICATION_NAME` | `finrl-trader` | visible in `pg_stat_activity` |
| `REQUIRE_DURABLE_STORE` | `true` (production) | refuses to start without a working store |

Rules enforced in `trader/data/db_store.py`:

- Only `postgresql://` / `postgres://` URIs are accepted.
- `sslmode=require` is appended for any non-local host that omits it.
- The DSN is never logged; errors are sanitized through `safe_msg`.
- With `require_durable=true`, a failed connection raises instead of silently
  falling back to in-memory storage.
- Database access is serialized with an `RLock` and a stale connection is
  re-established on the next health check.

## 3. Apply and verify the schema

From the repository root, with the pooled DSN in `DATABASE_URL`:

```powershell
.\.venv\Scripts\python.exe scripts\db_migrate.py
```

The script is idempotent and **additive only** (`CREATE TABLE IF NOT EXISTS`,
`CREATE INDEX IF NOT EXISTS`, schema-version upsert). It never issues
`DROP`/`DELETE` and never touches another database, so the Supabase source
cannot be overwritten.

Expected output shape:

```
db_migrate: target=neon-pooled backend=postgresql
db_migrate: health=ok latency_ms=...
db_migrate: schema_ok=True version=1 expected=1
db_migrate: tables={'signals': True, 'backtests': True, 'orders': True, 'schema_migrations': True}
db_migrate: counts={'signals': 0, 'backtests': 0, 'orders': 0, 'schema_migrations': 1}
```

Exit codes: `0` verified, `1` verification failed, `2` config/connection
failure. Use `--json` for machine-readable output (still DSN-free).

The API exposes the same evidence at `GET /api/v2/db` (sanitized: no host, user
or password) and in `GET /api/v2/health`.

## 4. Schema

| Table | Purpose |
|---|---|
| `signals` | strategy signal snapshots (symbol, timeframe, score, payload, timestamp) |
| `backtests` | completed backtest metrics and equity curve |
| `orders` | client order id, symbol, side, notional/qty, status, timestamps |
| `schema_migrations` | applied schema version (`version = 1`) |

Indexes cover the query paths used by the dashboard (recent-first by symbol and
timestamp). The schema is created by `DBStore._ensure_schema()` and mirrored by
`scripts/db_migrate.py` verification.

## 5. Railway configuration

Set these as Railway **service variables** (never in `railway.toml`, which
cannot carry environment variables):

```
DATABASE_URL=<Neon pooled DSN with ?sslmode=require>
REQUIRE_DURABLE_STORE=true
CORS_ORIGINS=https://<your-pages-project>.pages.dev
TRUST_PROXY_HEADERS=true
DASHBOARD_PASSWORD=<strong password>
ALPACA_API_KEY=<paper key>
ALPACA_API_SECRET=<paper secret>
```

The image already bakes `TRADING_MODE=paper`, `ENABLE_LIVE_TRADING=false`,
`ENABLE_PAPER_ORDERS=false` and `REQUIRE_DURABLE_STORE=true`. Do not override
them.

Deployment itself is **not** performed by this repository change; see
`docs/DEPLOYMENT_RAILWAY.md` and obtain explicit approval before deploying.

## 6. Verification checklist

```powershell
# 1. Schema and connectivity (no secrets printed)
.\.venv\Scripts\python.exe scripts\db_migrate.py

# 2. Hermetic backend tests (no DB, no broker, no network)
.\.venv\Scripts\python.exe -m pytest trader\tests -q

# 3. Local API smoke test (uses the real Neon store)
.\.venv\Scripts\python.exe scripts\run_trader.py
#    then: GET http://127.0.0.1:8000/api/v2/health
#          GET http://127.0.0.1:8000/api/v2/db
```

`/api/v2/health` must report the `db` component as `ok`; `/api/v2/db` must show
`schema_ok=true`, `version=1` and the four tables.

## 7. Rollback

The previous Supabase DSN is preserved (commented) in the local `.env` and its
Session Pooler form is documented in `docs/DEPLOYMENT_RAILWAY.md` section 8.2.
To roll back:

1. Set `DATABASE_URL` back to the Supabase Session Pooler DSN.
2. Keep `REQUIRE_DURABLE_STORE=true`.
3. Restart the service and re-run `scripts/db_migrate.py` (additive; it will
   create the same tables in Supabase if they are missing).

**Do not delete or overwrite the Supabase project or its data.** Neon and
Supabase are independent stores; no automatic two-way sync exists.

## 8. Known limitations

- **No data migration.** The Neon store starts empty (schema only). Historical
  Supabase rows are not copied; this was an explicit decision.
- **Railway egress unverified.** DNS confirms IPv4 records for the pooled host,
  but an actual Railway -> Neon connection has not been exercised from this
  environment.
- **Free-tier autosuspend.** A cold Neon branch adds latency to the first
  connection; `DB_CONNECT_TIMEOUT=5` and three retries keep startup bounded.
- **Pooled sessions.** Neon's pooler does not support every session-level
  feature. The app uses short statements plus additive DDL only; avoid
  long-lived `LISTEN/NOTIFY`, advisory-lock sessions or `SET`-heavy workflows on
  the pooled endpoint.
- **Single writer.** `DBStore` serializes access in-process; running multiple
  API replicas is supported by PostgreSQL but the in-process risk state and job
  queue are per replica.

## 9. Security

- `.env` is gitignored; `.env.example` contains placeholders only.
- The DSN, host, user and password are never logged or returned by the API.
- `scripts/db_migrate.py` prints only the target *label* (`neon-pooled`).
- Do not paste connection strings into issues, chat, dashboards or CI logs.
- Rotate the Neon role password if a DSN is ever exposed, then update the
  Railway variable and restart the service.
