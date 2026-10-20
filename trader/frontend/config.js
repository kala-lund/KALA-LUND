// FinRL Trader V2 - runtime frontend configuration.
//
// This file is checked in with an EMPTY API base URL, which means "same origin".
// That is correct for the self-hosted FastAPI deployment (backend serves the
// frontend at http://127.0.0.1:8000/).
//
// For Cloudflare Pages (frontend on a different origin than the API), the
// Pages build step regenerates this file with the Railway backend URL:
//     API_BASE_URL=https://<your-railway-domain> node scripts/build_frontend.js
// or set `API_BASE_URL` as a Cloudflare Pages build environment variable.
//
// This file contains no secrets. Never put credentials here.

window.FINRL_API_BASE_URL = "";
