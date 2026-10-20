# Deployment: Cloudflare Pages (Frontend)

**Target:** Cloudflare Pages
**Service:** FinRL Trader V2 dashboard (static assets only)
**Backend:** FastAPI on Railway — see [`DEPLOYMENT_RAILWAY.md`](DEPLOYMENT_RAILWAY.md)

The Python backend is **not** deployed to Cloudflare Workers. It stays on
Railway; Cloudflare hosts only the static dashboard.

---

## 1. What is deployed

`trader/frontend/` contains plain HTML/CSS/JS — no bundler, no npm dependencies:

| File | Purpose |
|---|---|
| `trader/frontend/index.html` | dashboard shell |
| `trader/frontend/app.js` | vanilla-JS SPA, calls the backend API |
| `trader/frontend/styles.css` | dark quant theme |
| `trader/frontend/config.js` | runtime API origin (`window.FINRL_API_BASE_URL`) |
| `trader/frontend/_redirects` | Pages routing rules (asset rewrites + SPA fallback) |

`scripts/build_frontend.js` copies these into the output directory and
regenerates `config.js` from the `API_BASE_URL` environment variable. It uses
only the Node standard library — no `npm install` step is required.

## 2. Build settings (verified against the repo)

| Pages setting | Value |
|---|---|
| Framework preset | None |
| Build command | `node scripts/build_frontend.js` |
| Build output directory | `dist` |
| Root directory | repository root |
| Node version | 20 (or newer) |
| Build environment variable | `API_BASE_URL=https://<your-railway-service>.up.railway.app` |

`wrangler.toml` mirrors the output directory (`pages_build_output_dir = "dist"`).

## 3. Deploy

### Option A — Cloudflare Pages (Git integration, recommended)

1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Connect to Git**
   → select this repository.
2. Set the build settings from the table above.
3. Add the build environment variable `API_BASE_URL` pointing at the Railway
   backend origin.
4. Deploy. The project is served at `https://<project>.pages.dev`.

### Option B — CLI

```bash
# API_BASE_URL is optional for a dry local check
API_BASE_URL=https://<your-railway-service>.up.railway.app node scripts/build_frontend.js
npx wrangler pages deploy dist --project-name finrl-trader
```

### Option C — Self-hosted (no Cloudflare)

`python scripts/run_trader.py` serves the same files at `/` and
`/static/*` — leave `API_BASE_URL` empty so the dashboard uses the same origin.

## 4. API origin and CORS

- `index.html` resolves the API base in this order:
  `window.API_BASE_URL` → `window.FINRL_API_BASE_URL` (from `config.js`) → `""`
  (same origin).
- The backend applies `CORS_ORIGINS` to every `/api/*` and `/api/v2/*` route
  (`trader/api/server.py`). Set it on Railway to the exact frontend origin:

  ```
  CORS_ORIGINS=https://<project>.pages.dev,https://dashboard.example.com
  ```

- Do not use a wildcard: the order / kill-switch endpoints are password gated.
- Never put broker credentials or `DASHBOARD_PASSWORD` in the frontend. The
  frontend only ever receives the API origin.

## 5. Custom domain, DNS and HTTPS

1. Pages project → **Custom domains → Set up a domain** → add e.g.
   `dashboard.example.com`.
2. Cloudflare shows the required CNAME/record. If the zone is already on
   Cloudflare, the record is created automatically; otherwise add it at the
   registrar and point it at `<project>.pages.dev`.
3. HTTPS is issued automatically by Cloudflare (TLS + HTTP/2 + HTTP/3). No
   certificate has to be obtained manually.
4. After the custom domain is live, add it to the Railway `CORS_ORIGINS`
   variable and redeploy the backend.

**DNS and traffic changes are performed manually by the owner.** No DNS record
is created or modified by this repository.

## 6. Routing notes

The dashboard HTML references `/static/app.js`, `/static/styles.css` and
`/static/config.js` because that is where FastAPI mounts the static folder.
`_redirects` maps those paths to the site root on Pages, and the final
`/*  /index.html  200` rule provides the SPA fallback:

```
/static/config.js   /config.js   200
/static/styles.css  /styles.css  200
/static/app.js      /app.js      200
/*                 /index.html   200
```

## 7. Verification after deploy

```bash
curl -s https://<project>.pages.dev/config.js          # -> window.FINRL_API_BASE_URL = "https://..."
curl -s https://<project>.pages.dev/static/app.js      # -> 200 (asset rewrite works)
curl -s https://<railway-host>/api/v2/health           # -> {"ok": true, "mode": "paper", ...}
```

Then open the dashboard in a browser: the status pill must show `PAPER`, and
the browser console must show no CORS error.

## 8. Manual dashboard actions

1. Create the Pages project and connect the Git repository.
2. Set build command / output directory / `API_BASE_URL`.
3. Add the custom domain and update Railway `CORS_ORIGINS`.
4. No DNS or traffic changes are performed automatically.

## 9. Notes

- The frontend contains no secrets and no broker calls; all trading decisions
  are made server side.
- The `PAPER` badge in the header is informational — the real enforcement is
  the backend configuration validation described in `DEPLOYMENT_RAILWAY.md`.
