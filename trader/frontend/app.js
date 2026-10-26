/* KALA-LUND paper-trading terminal.
 * Vanilla ES2020 + SVG charts. No build step, no third-party runtime code.
 * All values shown come from the backend; missing data renders as "—".
 */
(() => {
  "use strict";

  const API_BASE = String(window.API_BASE_URL || window.FINRL_API_BASE_URL || "").replace(/\/+$/, "");
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const TABS = [
    { id: "overview", label: "Overview", icon: "◫", sub: "Portfolio, risk and system state" },
    { id: "portfolio", label: "Portfolio", icon: "◈", sub: "Holdings, allocation, orders and fills" },
    { id: "markets", label: "Markets", icon: "⌁", sub: "Watchlist quotes and candlestick chart" },
    { id: "strategies", label: "Strategies", icon: "⌁", sub: "Signal aggregation and Muse status" },
    { id: "backtest", label: "Backtest", icon: "◷", sub: "Equity curve, drawdown, benchmark, walk-forward" },
    { id: "risk", label: "Risk", icon: "⛨", sub: "Limits, exposure and emergency stop" },
    { id: "activity", label: "Activity", icon: "▤", sub: "Server events and browser errors" },
    { id: "settings", label: "Settings", icon: "⚙", sub: "Safe configuration and connection details" },
  ];

  const state = {
    view: "overview",
    config: null,
    health: null,
    db: null,
    // The dashboard password lives in this object only: never localStorage,
    // sessionStorage, cookies or the URL. It leaves the tab exclusively through
    // the X-Dashboard-Password header and the existing POST JSON bodies.
    pwd: "",
    ops: false,
    authState: "unknown", // unknown | unlocked | locked | throttled
    authNote: "",
    authToast: "",
    authCooldownUntil: 0,
    theme: localStorage.getItem("kl-theme") || "dark",
    lastJob: null,
    jobPoll: null,
    errors: [],
    updatedAt: null,
  };

  let viewSeq = 0;
  let pollTimer = null;
  const getCache = new Map();
  const inflight = new Map();
  const FETCH_TIMEOUT = 20000;
  // Cache scopes keep authenticated and anonymous reads of the same path apart:
  // an unlocked response must never be served to a locked session, or the other
  // way round.
  const CACHE_AUTH = "auth:";
  const CACHE_ANON = "anon:";
  const AUTH_HEADER = "X-Dashboard-Password";
  const LOCKED_MSG = "Account data is locked. Enter the dashboard password to view balances, positions, orders and logs.";

  /* ---------------- formatting ---------------- */
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function num(v, dec = 2) {
    if (!isNum(v)) return "—";
    return v.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }
  function money(v, dec = 2) {
    if (!isNum(v)) return "—";
    return (v < 0 ? "-" : "") + "$" + Math.abs(v).toLocaleString(undefined, {
      minimumFractionDigits: dec, maximumFractionDigits: dec,
    });
  }
  function signedMoney(v, dec = 2) {
    if (!isNum(v)) return "—";
    return (v > 0 ? "+" : "") + money(v, dec);
  }
  function pctPts(v, dec = 2) {
    if (!isNum(v)) return "—";
    return (v > 0 ? "+" : "") + v.toFixed(dec) + "%";
  }
  function pctFrac(v, dec = 2) {
    if (!isNum(v)) return "—";
    return pctPts(v * 100, dec);
  }
  function qty(v) {
    if (!isNum(v)) return "—";
    return v.toLocaleString(undefined, { maximumFractionDigits: 6 });
  }
  function tone(v) {
    if (!isNum(v) || v === 0) return "";
    return v > 0 ? "ok" : "bad";
  }
  function fmtTs(ts) {
    if (!isNum(ts) || ts <= 0) return "—";
    const d = new Date(ts);
    return d.toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function timeAgo(ts) {
    if (!isNum(ts) || ts <= 0) return "—";
    const s = Math.max(0, (Date.now() - ts) / 1000);
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }
  function shortTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function tzName() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "local time"; }
    catch { return "local time"; }
  }
  function stamp(iso) {
    // Server timestamps are UTC ISO-8601; show them in the browser's zone and
    // label the zone so a reader never has to guess.
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return `${d.toLocaleString()} ${tzName()}`;
  }

  /* ---------------- errors / toasts ---------------- */
  function recordError(where, err) {
    state.errors.push({ ts: Date.now(), where, msg: String((err && err.message) || err).slice(0, 300) });
    if (state.errors.length > 60) state.errors.shift();
  }

  function toast(message, kind = "") {
    const host = $("#toasts");
    if (!host) return;
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = String(message).slice(0, 400);
    host.appendChild(el);
    setTimeout(() => el.remove(), 7000);
  }

  function apiError(message, status = 0, code = "") {
    const e = new Error(message);
    e.status = status;
    e.code = code;
    return e;
  }

  /* ---------------- HTTP ---------------- */
  function cacheScope(auth) {
    // Only a request that will actually carry the header may use the
    // authenticated cache scope.
    return auth && state.pwd ? CACHE_AUTH : CACHE_ANON;
  }

  function cacheKey(path, auth) {
    return cacheScope(auth) + path;
  }

  function clearCache({ authOnly = false, anonOnly = false } = {}) {
    for (const key of Array.from(getCache.keys())) {
      if (authOnly && !key.startsWith(CACHE_AUTH)) continue;
      if (anonOnly && !key.startsWith(CACHE_ANON)) continue;
      getCache.delete(key);
    }
  }

  async function rawRequest(path, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), opts.timeout || FETCH_TIMEOUT);
    const headers = {};
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    // Account-data reads send the dashboard credential as a header only, and
    // only while unlocked. It is never appended to the path or a query string.
    if (opts.auth && state.pwd) headers[AUTH_HEADER] = state.pwd;
    let res;
    let text = "";
    try {
      res = await fetch(API_BASE + path, {
        method,
        headers: Object.keys(headers).length ? headers : undefined,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        signal: ctrl.signal,
        cache: "no-store",
      });
      text = await res.text();
    } catch (e) {
      clearTimeout(timer);
      if (e && e.name === "AbortError") throw apiError(`Request timed out after ${(opts.timeout || FETCH_TIMEOUT) / 1000}s`);
      throw apiError(`Network error: ${String((e && e.message) || e)}`);
    }
    clearTimeout(timer);
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = { detail: text }; }
    if (!res.ok) {
      const detail = body && body.detail !== undefined ? body.detail : text;
      const msg = (body && body.code ? `[${body.code}] ` : `[${res.status}] `) +
        (typeof detail === "string" ? detail : JSON.stringify(detail).slice(0, 400));
      throw apiError(msg, res.status, (body && body.code) || "");
    }
    return body;
  }

  async function apiGet(path, { ttl = 5000, force = false, timeout = FETCH_TIMEOUT, auth = false } = {}) {
    const key = cacheKey(path, auth);
    const hit = getCache.get(key);
    if (!force && hit && Date.now() - hit.t < ttl) return hit.v;
    if (inflight.has(key)) return inflight.get(key);
    const run = (async () => {
      let lastErr;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const value = await rawRequest(path, { timeout, auth });
          getCache.set(key, { t: Date.now(), v: value });
          return value;
        } catch (e) {
          lastErr = e;
          // 403 (locked / rejected credential) and 429 (throttled) are terminal:
          // retrying a credential failure cannot succeed and would turn the poll
          // loop into a password oracle.
          const retryable = !e.status || e.status >= 500;
          if (!retryable || attempt === 1) break;
          await new Promise((r) => setTimeout(r, 400));
        }
      }
      throw lastErr;
    })().finally(() => inflight.delete(key));
    inflight.set(key, run);
    return run;
  }

  async function apiPost(path, body, { timeout = 30000, auth = false } = {}) {
    // Mutations are never retried blindly: a duplicate POST could place an order twice.
    return rawRequest(path, { method: "POST", body, timeout, auth });
  }

  async function safeGet(path, opts) {
    try {
      return { ok: true, value: await apiGet(path, opts) };
    } catch (e) {
      recordError(path, e);
      return { ok: false, error: e };
    }
  }

  /* ---------------- auth state ---------------- */
  function passwordRequired() {
    return !!(state.config && state.config.password_required === true);
  }

  function isLocked() {
    // Fail closed only when the server told us a credential exists, or when a
    // gated read already answered 403 (config may be unreachable). A local
    // install without DASHBOARD_PASSWORD keeps working exactly as before.
    if (!state.pwd && state.authState === "locked") return true;
    return passwordRequired() && !state.pwd;
  }

  function isThrottled() {
    return state.authCooldownUntil > Date.now();
  }

  function authError(e) {
    return !!e && (e.status === 403 || e.status === 429);
  }

  function authNote() {
    if (state.authState === "throttled" && isThrottled()) {
      const secs = Math.max(1, Math.ceil((state.authCooldownUntil - Date.now()) / 1000));
      return `${state.authNote || "Credential attempts throttled."} Retry in ${secs}s.`;
    }
    if (state.authState === "locked" && state.authNote) return state.authNote;
    return "";
  }

  function noteAuthFailure(e) {
    const status = e && e.status;
    const msg = String((e && e.message) || "credential rejected");
    if (status === 429) {
      // Back off for longer than the server window instead of hammering it.
      state.authState = "throttled";
      state.authNote = "Too many credential attempts.";
      state.authCooldownUntil = Date.now() + 60000;
    } else {
      state.authState = "locked";
      state.authNote = /invalid password/i.test(msg)
        ? "The server rejected the dashboard password. Unlock again with the current password."
        : "Account data requires the dashboard password.";
      state.authCooldownUntil = 0;
      // A credential the server rejects is useless: drop it so the next poll
      // renders the locked placeholder instead of repeating the 403.
      if (state.pwd || state.ops) {
        state.pwd = "";
        state.ops = false;
        updateLockButton();
      }
      clearCache({ authOnly: true });
    }
    if (state.authToast !== state.authNote) {
      state.authToast = state.authNote;
      toast(state.authNote, "warn");
    }
  }

  async function gatedGet(path, opts = {}) {
    // Gated account reads: skipped entirely while locked or throttled so the
    // polling loop never sends an unauthenticated request it knows will fail.
    if (isLocked()) {
      return { ok: false, locked: true, error: apiError("Account data is locked.") };
    }
    if (isThrottled()) {
      return { ok: false, throttled: true, error: apiError(authNote()) };
    }
    const res = await safeGet(path, { ...opts, auth: true });
    if (!res.ok && authError(res.error)) {
      noteAuthFailure(res.error);
      return { ok: false, locked: res.error.status === 403, throttled: res.error.status === 429, error: res.error };
    }
    return res;
  }

  function authBlocked(...results) {
    return results.some((r) => r && (r.locked || r.throttled));
  }

  /* ---------------- chart renderers ---------------- */
  function emptyChart(text = "No data", height = 200) {
    return `<div class="empty" style="min-height:${height}px">${esc(text)}</div>`;
  }

  function lineChart({ series, height = 220, yFormat = (v) => num(v, 0), ariaLabel = "Line chart" }) {
    const usable = series.filter((s) => s.values.some(isNum));
    const all = usable.flatMap((s) => s.values).filter(isNum);
    if (!all.length) return emptyChart("No series data", height);
    const W = 760;
    const H = height;
    const P = { l: 58, r: 14, t: 12, b: 24 };
    let min = Math.min(...all);
    let max = Math.max(...all);
    if (min === max) { min -= 1; max += 1; }
    const pad = (max - min) * 0.08;
    min -= pad; max += pad;
    const len = Math.max(...usable.map((s) => s.values.length));
    const X = (i) => P.l + (len <= 1 ? 0 : (i / (len - 1)) * (W - P.l - P.r));
    const Y = (v) => P.t + (1 - ((v - min) / (max - min))) * (H - P.t - P.b);
    let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(ariaLabel)}" preserveAspectRatio="none"><title>${esc(ariaLabel)}</title>`;
    for (let k = 0; k <= 3; k++) {
      const v = min + ((max - min) * k) / 3;
      const y = Y(v).toFixed(2);
      svg += `<line class="grid-line" x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}"></line>`;
      svg += `<text x="4" y="${(Number(y) + 3).toFixed(2)}">${esc(yFormat(v))}</text>`;
    }
    usable.forEach((s) => {
      let d = "";
      let pen = false;
      s.values.forEach((v, i) => {
        if (!isNum(v)) { pen = false; return; }
        d += `${pen ? "L" : "M"}${X(i).toFixed(2)},${Y(v).toFixed(2)} `;
        pen = true;
      });
      if (s.fill && d) {
        const base = Y(Math.max(min, 0)).toFixed(2);
        const area = `${d}L${X(s.values.length - 1).toFixed(2)},${base} L${X(0).toFixed(2)},${base} Z`;
        svg += `<path class="area" d="${area}" fill="${s.color}"></path>`;
      }
      if (d) {
        svg += `<path class="${s.dash ? "bench" : "line"}" d="${d}" stroke="${s.color}"></path>`;
      }
    });
    svg += `<line class="axis" x1="${P.l}" y1="${H - P.b}" x2="${W - P.r}" y2="${H - P.b}"></line>`;
    if (usable[0]) {
      for (let i = 0; i < usable[0].values.length; i += Math.max(1, Math.floor(len / 5))) {
        const v = usable[0].values[i];
        if (!isNum(v)) continue;
        svg += `<text x="${X(i).toFixed(2)}" y="${H - 6}" text-anchor="middle">${i + 1}</text>`;
      }
    }
    svg += "</svg>";
    return svg;
  }

  function drawdownSeries(values) {
    const out = [];
    let peak = 0;
    values.forEach((v) => {
      if (!isNum(v) || v <= 0) { out.push(null); return; }
      peak = Math.max(peak, v);
      out.push(peak > 0 ? -((peak - v) / peak) * 100 : 0);
    });
    return out;
  }

  function candleChart(bars, ariaLabel) {
    const valid = (bars || []).filter((b) => b && isNum(b.o) && isNum(b.h) && isNum(b.l) && isNum(b.c));
    if (!valid.length) return emptyChart("No candles", 240);
    const W = 760, H = 260, P = { l: 8, r: 58, t: 12, b: 22 };
    let hi = Math.max(...valid.map((b) => b.h));
    let lo = Math.min(...valid.map((b) => b.l));
    if (hi === lo) { hi += 1; lo -= 1; }
    const pad = (hi - lo) * 0.05;
    hi += pad; lo -= pad;
    const Y = (p) => P.t + (1 - (p - lo) / (hi - lo)) * (H - P.t - P.b);
    const cw = (W - P.l - P.r) / valid.length;
    const bw = Math.max(1, cw * 0.62);
    let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(ariaLabel)}" preserveAspectRatio="none"><title>${esc(ariaLabel)}</title>`;
    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4;
      const y = Y(v).toFixed(2);
      svg += `<line class="grid-line" x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}"></line>`;
      svg += `<text x="${W - P.r + 4}" y="${(Number(y) + 3).toFixed(2)}">${esc(lo > 0 ? v.toFixed(v < 10 ? 4 : 2) : v.toFixed(2))}</text>`;
    }
    valid.forEach((b, i) => {
      const x = P.l + i * cw + cw / 2;
      const up = b.c >= b.o;
      const color = up ? "var(--ok)" : "var(--bad)";
      svg += `<line x1="${x.toFixed(2)}" y1="${Y(b.h).toFixed(2)}" x2="${x.toFixed(2)}" y2="${Y(b.l).toFixed(2)}" stroke="${color}" stroke-width="1"></line>`;
      const y1 = Y(Math.max(b.o, b.c));
      const y2 = Y(Math.min(b.o, b.c));
      svg += `<rect x="${(x - bw / 2).toFixed(2)}" y="${y1.toFixed(2)}" width="${bw.toFixed(2)}" height="${Math.max(1, y2 - y1).toFixed(2)}" fill="${color}"></rect>`;
    });
    svg += "</svg>";
    return svg;
  }

  function allocationBlock(items) {
    if (!items.length) return `<div class="empty">No positions. Allocation appears when the paper account holds assets.</div>`;
    return `<div class="alloc">${items.map((it) => `
      <div class="alloc-row">
        <span class="sym" title="${esc(it.symbol)}">${esc(it.symbol)}</span>
        <span class="alloc-track"><span class="alloc-fill" style="width:${Math.max(0, Math.min(100, it.pct)).toFixed(2)}%"></span></span>
        <span class="num">${it.pct.toFixed(1)}%</span>
      </div>`).join("")}</div>`;
  }

  function kpi(label, value, sub, kind = "", missing = false) {
    return `<div class="kpi card ${kind}">
      <div class="k-label">${esc(label)}</div>
      <div class="k-value ${missing ? "is-missing" : ""}">${esc(value)}</div>
      <div class="k-sub">${esc(sub || "")}</div>
    </div>`;
  }

  function errorBox(what, error) {
    return `<div class="error-state"><strong>${esc(what)} unavailable</strong><span>${esc(error && error.message ? error.message : error)}</span></div>`;
  }

  function banner(kind, text) {
    return `<div class="banner ${kind}">${esc(text)}</div>`;
  }

  function cmpClass(v, invert = false) {
    if (!isNum(v) || v === 0) return "";
    const good = invert ? v < 0 : v > 0;
    return good ? "ok" : "bad";
  }

  /* ---------------- shared fragments ---------------- */
  function badge(kind, text) {
    // Text always accompanies the colour: never status by colour alone.
    const cls = kind === "ok" ? "ok" : kind === "bad" ? "bad" : kind === "warn" ? "warn" : "mut";
    return `<span class="pill ${cls}">${esc(text)}</span>`;
  }

  function kvRow(label, value) {
    return `<div class="kv"><span class="k">${esc(label)}</span><span class="v">${esc(value === undefined || value === null || value === "" ? "—" : String(value))}</span></div>`;
  }

  function latencyText(v) {
    return isNum(v) ? `${num(v, 0)} ms` : "—";
  }

  function componentBadge(c) {
    if (!c || typeof c.ok !== "boolean") return badge("mut", "unknown");
    return c.ok ? badge("ok", "ok") : badge("warn", "degraded");
  }

  function brokerStatusText(status) {
    switch (status) {
      case "ok": return "reachable";
      case "missing_credentials": return "credentials not configured";
      case "auth_failed": return "credentials rejected (401/403)";
      case "timeout": return "timed out";
      case "network_error": return "network error";
      case "tls_error": return "TLS error";
      case "broker_error": return "broker error";
      case "probing": return "probing";
      default: return "unknown";
    }
  }

  function notReadyReason(h) {
    const storage = h.storage || {};
    const comps = h.components || {};
    if (storage.required && !storage.durable) return "not ready — durable store unavailable (HTTP 503 to /healthz)";
    if (comps.config && comps.config.ok === false) return "not ready — configuration invalid (HTTP 503 to /healthz)";
    return "not ready — this instance cannot serve traffic (HTTP 503 to /healthz)";
  }

  function degradedReason(h) {
    const comps = h.components || {};
    const failing = Object.entries(comps).filter(([, c]) => c && c.ok === false).map(([name]) => name);
    return failing.length ? `degraded: ${failing.join(", ")}` : "reported degraded without a failing component";
  }

  function healthRow(name, c) {
    if (!c) {
      return `<div class="health-row">
        <span class="h-name">${esc(name)}</span>
        <span>${badge("mut", "unknown")}</span>
        <span class="h-detail">no component data</span>
      </div>`;
    }
    const detail = c.detail || (c.ok ? "ok" : "no detail reported");
    const bits = [detail];
    if (c.configured === false && c.ok) bits.push("not configured");
    if (isNum(c.latency_ms)) bits.push(`${latencyText(c.latency_ms)}`);
    return `<div class="health-row">
      <span class="h-name">${esc(name)}</span>
      <span>${componentBadge(c)}</span>
      <span class="h-detail">${esc(bits.join(" · "))}${c.error ? ` — ${esc(c.error)}` : ""}</span>
    </div>`;
  }

  function healthRows(health) {
    if (!health || !health.components) return `<div class="empty">Health unavailable.</div>`;
    const h = health;
    const comps = h.components;
    const storage = h.storage || {};
    const broker = comps.broker || {};
    const redis = comps.redis || {};
    const ready = h.ready === true;
    const healthy = h.ok === true;
    const rows = Object.entries(comps).map(([name, c]) => healthRow(name, c)).join("");
    return `<div class="health-rows" aria-live="polite" aria-atomic="false">
      ${rows}
      <div class="health-row">
        <span class="h-name">Readiness</span>
        <span>${ready ? badge("ok", "ready") : badge("bad", "not ready")}</span>
        <span class="h-detail">${esc(ready ? "instance can take traffic" : notReadyReason(h))}</span>
      </div>
      <div class="health-row">
        <span class="h-name">Overall</span>
        <span>${healthy ? badge("ok", "healthy") : badge("warn", "degraded")}</span>
        <span class="h-detail">${esc(healthy ? "all components report ok" : degradedReason(h))}</span>
      </div>
    </div>
    <div class="health-facts">
      ${kvRow("Mode", h.mode === "paper" ? "paper only" : (h.mode || "—"))}
      ${kvRow("Snapshot", h.ts ? stamp(h.ts) : "—")}
      ${kvRow("Store backend", `${storage.backend || "—"}${storage.durable ? " (durable)" : " (not durable)"}`)}
      ${kvRow("Store required", storage.required === true ? "yes — readiness fails without it" : storage.required === false ? "no" : "—")}
      ${kvRow("Store latency", latencyText(comps.db && comps.db.latency_ms))}
      ${kvRow("Cache backend", redis.backend || "—")}
      ${kvRow("Cache fallback", redis.fallback === true ? "in-process cache fallback" : redis.fallback === false ? "none" : "unknown")}
      ${kvRow("Cache latency", latencyText(redis.latency_ms))}
      ${kvRow("Broker endpoint", broker.endpoint ? `${broker.endpoint} · paper` : "—")}
      ${kvRow("Broker status", brokerStatusText(broker.status))}
      ${kvRow("Broker account", broker.account_status || "—")}
      ${kvRow("Broker latency", latencyText(broker.latency_ms))}
      ${kvRow("Broker probe age", isNum(broker.stale_seconds) ? `${num(broker.stale_seconds, 1)}s ago` : "—")}
    </div>`;
  }

  function lockedPanel(title, hint) {
    const note = authNote();
    return `<div class="locked" role="group" aria-label="${esc(title || "Account data")} locked">
      <div class="row"><span class="pill warn">locked</span><strong>${esc(title || "Account data")}</strong></div>
      <p class="muted small">${esc(hint || LOCKED_MSG)}</p>
      ${note ? `<p class="muted small">${esc(note)}</p>` : ""}
      <div class="row"><button class="btn primary small" type="button" data-unlock="1">Unlock dashboard</button></div>
    </div>`;
  }

  function brokerUnavailable(what, error) {
    return `<div class="error-state" role="status">
      <strong>${esc(what)} unavailable</strong>
      <span>${esc(error || "The paper broker did not return account data.")}</span>
      <span class="muted small">Shown as unavailable, never as a zero balance or an empty book.</span>
    </div>`;
  }

  function quoteRow(q) {
    if (!q.available) {
      return `<tr><td class="sym">${esc(q.symbol)}</td><td colspan="4" class="muted">Unavailable — ${esc(q.error || "no data")}</td></tr>`;
    }
    const ql = q.quality || {};
    const quality = ql.ok ? `<span class="pill ok">fresh</span>` : `<span class="pill warn" title="${esc((ql.issues || []).join("; "))}">stale</span>`;
    return `<tr>
      <td class="sym">${esc(q.symbol)}</td>
      <td class="num">${money(q.last, q.last < 10 ? 4 : 2)}</td>
      <td class="num ${cmpClass(q.change)}">${signedMoney(q.change, q.last < 10 ? 4 : 2)}</td>
      <td class="num ${cmpClass(q.change_pct)}">${pctPts(q.change_pct)}</td>
      <td>${quality}</td>
    </tr>`;
  }

  /* ---------------- auth ---------------- */
  function openAuth() {
    const modal = $("#authModal");
    if (!modal) return;
    modal.hidden = false;
    const app = $(".app");
    if (app) app.inert = true;
    const input = $("#authPwd");
    if (input) { input.value = ""; setTimeout(() => input.focus(), 30); }
    $("#authErr").hidden = true;
  }

  function closeAuth() {
    const modal = $("#authModal");
    if (modal) modal.hidden = true;
    const app = $(".app");
    if (app) app.inert = false;
  }

  async function unlock(password) {
    const res = await apiPost("/api/auth", { password });
    state.pwd = password;
    state.ops = true;
    state.authState = "unlocked";
    state.authNote = "";
    state.authToast = "";
    state.authCooldownUntil = 0;
    closeAuth();
    updateLockButton();
    // Anonymous cache entries must not survive the unlock: gated panels have to
    // re-fetch with the credential.
    clearCache();
    toast("Unlocked for this tab.", "ok");
    return res;
  }

  function ensureOps(what = "trading controls") {
    if (state.ops && state.pwd) return true;
    if (state.config && state.config.password_required === false) {
      toast("Trading controls are locked: DASHBOARD_PASSWORD is not configured on the server.", "warn");
      return false;
    }
    toast(`Unlock the dashboard to use ${what}.`, "warn");
    openAuth();
    return false;
  }

  function updateLockButton() {
    const b = $("#btnLock");
    if (!b) return;
    b.textContent = state.ops ? "Lock" : "Unlock";
    b.setAttribute("aria-pressed", state.ops ? "true" : "false");
    b.title = state.ops
      ? "Clear the in-memory password and hide account data"
      : "Enter the dashboard password to load account data and enable controls";
  }

  /* ---------------- chrome ---------------- */
  function buildNav() {
    const nav = $("#nav");
    if (!nav) return;
    nav.innerHTML = TABS.map((t) => `
      <button type="button" data-tab="${t.id}" aria-current="${state.view === t.id ? "page" : "false"}">
        <span class="nav-ico" aria-hidden="true">${t.icon}</span><span>${esc(t.label)}</span>
      </button>`).join("");
    $$("button[data-tab]", nav).forEach((b) => {
      b.addEventListener("click", () => {
        if (state.view === b.dataset.tab) return;
        state.view = b.dataset.tab;
        localStorage.setItem("kl-view", state.view);
        render();
      });
    });
  }

  function setTheme(theme) {
    state.theme = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", state.theme);
    localStorage.setItem("kl-theme", state.theme);
    const btn = $("#themeToggle");
    if (btn) {
      btn.setAttribute("aria-pressed", state.theme === "dark" ? "true" : "false");
      const label = $("#themeLabel");
      if (label) label.textContent = state.theme === "dark" ? "Dark" : "Light";
    }
  }

  function setDot(id, status, title) {
    const el = document.getElementById(id);
    if (!el) return;
    const dot = $(".dot", el);
    if (dot) dot.className = "dot " + (status || "");
    if (title) el.title = title;
  }

  function updateChrome(health, config) {
    const h = health || state.health;
    const cfg = config || state.config;
    const ok = !!(h && h.ok);
    const ready = !!(h && h.ready);
    const conn = $("#conn");
    if (conn) {
      const dot = $(".dot", conn);
      // Three distinct states: healthy, degraded (ready but a component is off),
      // and dead/unreachable. Never render degraded as "Live".
      if (dot) dot.className = "dot " + (h ? (ok ? "ok" : ready ? "warn" : "bad") : "bad");
      const label = $("span:last-child", conn);
      if (label) label.textContent = h ? (ok ? "Live" : ready ? "Degraded" : "Not ready") : "Offline";
      conn.title = h
        ? `healthy ${ok ? "yes" : "no"} · ready ${ready ? "yes" : "no"} · mode ${h.mode || "unknown"} · ${h.ts ? stamp(h.ts) : "no timestamp"}`
        : "no health response yet";
    }
    if (h) {
      setDot("miniApi", ok ? "ok" : ready ? "warn" : "bad",
        `${ok ? "healthy" : "degraded"} · ready ${ready ? "yes" : "no"}${h.ts ? " · " + stamp(h.ts) : ""}`);
      const dbc = h.components && h.components.db;
      setDot("miniDb", dbc ? (dbc.ok ? "ok" : "warn") : "", dbc ? String(dbc.detail || "") : "");
      const rc = h.components && h.components.redis;
      setDot("miniRedis", rc ? (rc.ok ? "ok" : (rc.configured ? "warn" : "")) : "", rc ? String(rc.detail || "") : "");
      const live = $("#liveStatus");
      if (live) {
        // Keep this region to state changes only: no timestamps, or a screen
        // reader would announce on every poll.
        live.textContent = `System ${ok ? "healthy" : "degraded"}; readiness ${ready ? "ready" : "not ready"}` +
          `; paper mode ${h.mode === "paper" ? "confirmed" : "unconfirmed"}`;
      }
    }
    if (cfg) {
      const mode = $("#statusMode");
      if (mode) mode.textContent = `mode ${cfg.trading_mode || "—"} · live ${cfg.live ? "ON" : "off"}`;
      const sym = $("#statusSymbols");
      if (sym) sym.textContent = `symbols ${cfg.n_symbols ?? "—"}`;
      const tf = $("#statusTimeframe");
      if (tf) tf.textContent = `timeframe ${cfg.timeframe || "—"}`;
    }
    const updated = $("#statusUpdated");
    if (updated) updated.textContent = `updated ${state.updatedAt ? shortTime(state.updatedAt) : "—"} ${tzName()}`;
    const statusDb = $("#statusDb");
    if (statusDb) statusDb.textContent = h && h.components && h.components.db ? `db ${h.components.db.detail}` : "db —";
    const statusAuth = $("#statusAuth");
    if (statusAuth) {
      statusAuth.textContent = state.pwd
        ? "auth unlocked"
        : passwordRequired() ? "auth locked" : (state.config ? "auth not required" : "auth —");
    }
  }

  async function refreshChrome() {
    const [h, cfg] = await Promise.all([
      safeGet("/api/health", { ttl: 8000 }),
      safeGet("/api/config/validate", { ttl: 30000 }),
    ]);
    if (h.ok) state.health = h.value;
    if (cfg.ok) state.config = cfg.value;
    updateChrome(h.ok ? h.value : null, cfg.ok ? cfg.value : null);
  }

  /* ---------------- views ---------------- */
  const VIEWS = {};

  VIEWS.overview = async (root, seq) => {
    root.innerHTML = `<div class="stack">
      <div class="skeleton" style="min-height:90px"></div>
      <div class="skeleton" style="min-height:120px"></div>
      <div class="skeleton" style="min-height:220px"></div>
    </div>`;
    const [health, cfg, summary, positions, perf, auto, quotes, db] = await Promise.all([
      safeGet("/api/health"), safeGet("/api/config/validate"), gatedGet("/api/v2/summary"),
      gatedGet("/api/v2/positions"), gatedGet("/api/v2/performance"), safeGet("/api/v2/automate/status"),
      safeGet("/api/v2/quotes"), safeGet("/api/v2/db"),
    ]);
    if (seq !== viewSeq) return;
    if (health.ok) state.health = health.value;
    if (cfg.ok) state.config = cfg.value;
    if (db.ok) state.db = db.value;
    updateChrome(health.ok ? health.value : null, cfg.ok ? cfg.value : null);

    const acctLocked = authBlocked(summary, positions);
    const perfLocked = authBlocked(perf);
    const s = summary.ok ? summary.value : null;
    const available = !!(s && s.available);
    const dbv = db.ok ? db.value : {};
    // Only an explicit ok=true is shown as healthy; a failed/absent report is
    // never coloured green.
    const dbOk = dbv.ok === true;

    const kpis = acctLocked
      ? ""
      : available
        ? [
          kpi("Equity", money(s.equity), s.source === "broker" ? "broker-reported · paper" : (s.status || "paper account"), "ok"),
          kpi("Cash", money(s.cash), "settled cash"),
          kpi("Buying power", money(s.buying_power), "available margin"),
          kpi("Day P&L", signedMoney(s.day_pnl), "broker session", tone(s.day_pnl)),
          kpi("Unrealized P&L", signedMoney(s.unrealized_pl), `${num(s.positions, 0)} position(s)`, tone(s.unrealized_pl)),
          kpi("Drawdown", pctPts(-Math.abs(s.drawdown_pct || 0)), `peak ${money(s.peak)}`, (s.drawdown_pct || 0) > 0 ? "warn" : ""),
        ].join("")
        : [
          kpi("Equity", "—", "broker unavailable", "", true),
          kpi("Cash", "—", "broker unavailable", "", true),
          kpi("Buying power", "—", "broker unavailable", "", true),
          kpi("Day P&L", "—", "broker unavailable", "", true),
          kpi("Unrealized P&L", "—", "broker unavailable", "", true),
          kpi("Drawdown", "—", "broker unavailable", "", true),
        ].join("");

    let alert = "";
    if (acctLocked) alert = "";
    else if (!summary.ok) alert = banner("bad", `Account summary unavailable: ${summary.error.message}`);
    else if (!available) alert = banner("warn", `Paper broker account not readable (${s.error || "no credentials"}). Values are shown as unavailable, never as zero.`);

    const acctMeta = acctLocked || !available
      ? ""
      : `<p class="muted small">Paper account · source ${esc(s.source || "unknown")} · as of ${esc(shortTime(s.as_of))} ${esc(tzName())} · values are broker-reported, not simulated.</p>`;

    // Equity / benchmark from the most recent completed backtest.
    let perfCard;
    if (perfLocked) {
      perfCard = lockedPanel("Performance history");
    } else if (!perf.ok) {
      perfCard = errorBox("Performance", perf.error);
    } else if (perf.value.source === "none") {
      perfCard = `<div class="empty"><strong>No backtest yet</strong><span>${esc(perf.value.message || "Run one from the Backtest tab.")}</span></div>`;
    } else {
      const p = perf.value;
      const curve = p.curve || [];
      const bench = p.benchmark || [];
      perfCard = `<div class="chart-wrap">${lineChart({
        series: [
          { values: curve, color: "var(--accent)", fill: true },
          { values: bench, color: "var(--faint)", dash: true },
        ],
        yFormat: (v) => money(v, 0),
        ariaLabel: `Equity curve for ${p.symbol || "backtest"}`,
      })}</div>
      <div class="legend"><span><i style="background:var(--accent)"></i>Strategy</span><span><i style="background:var(--faint)"></i>Buy &amp; hold</span></div>
      <div class="metric-list" style="margin-top:10px;">
        <div class="metric"><div class="k">Total return</div><div class="v ${tone(p.metrics && p.metrics.total_return)}">${pctFrac(p.metrics && p.metrics.total_return)}</div></div>
        <div class="metric"><div class="k">Sharpe</div><div class="v">${num(p.metrics && p.metrics.sharpe, 2)}</div></div>
        <div class="metric"><div class="k">Max drawdown</div><div class="v bad">${pctFrac(-Math.abs((p.metrics && p.metrics.max_dd) || 0))}</div></div>
        <div class="metric"><div class="k">Trades</div><div class="v">${num(p.metrics && p.metrics.trades, 0)}</div></div>
      </div>`;
    }

    // Allocation from real positions only.
    let alloc = `<div class="empty">Allocation unavailable.</div>`;
    if (positions.locked || positions.throttled) {
      alloc = lockedPanel("Allocation");
    } else if (!positions.ok) {
      alloc = errorBox("Positions", positions.error);
    } else if (!positions.value.available) {
      alloc = brokerUnavailable("Allocation", positions.value.error);
    } else if (positions.value.positions.length) {
      const rows = positions.value.positions;
      const total = rows.reduce((a, p) => a + Math.abs(Number(p.market_value) || 0), 0);
      alloc = allocationBlock(rows.map((p) => ({
        symbol: p.symbol,
        pct: total > 0 ? (Math.abs(Number(p.market_value) || 0) / total) * 100 : 0,
      })).sort((a, b) => b.pct - a.pct));
    } else {
      alloc = `<div class="empty">Flat book — no allocation to display.</div>`;
    }

    const quoteTable = quotes.ok
      ? (quotes.value.quotes.length
        ? `<div class="table-wrap"><table class="data"><caption class="sr-only">Watchlist quotes</caption>
            <thead><tr><th>Symbol</th><th class="num">Last</th><th class="num">Change</th><th class="num">%</th><th>Quality</th></tr></thead>
            <tbody>${quotes.value.quotes.map(quoteRow).join("")}</tbody></table></div>
          <p class="muted small" style="margin-top:6px">As of ${esc(stamp(quotes.value.as_of))} · ${esc(quotes.value.timeframe || "")}</p>`
        : `<div class="empty">No configured symbols.</div>`)
      : errorBox("Watchlist", quotes.error);

    const autoV = auto.ok ? auto.value : null;
    root.innerHTML = `<div class="stack">
      ${alert}
      ${acctLocked
        ? lockedPanel("Account metrics")
        : `<section class="grid kpis" aria-label="Account metrics">${kpis}</section>${acctMeta}`}
      <section class="grid two">
        <div class="card">
          <div class="card-head"><h2>Equity &amp; benchmark</h2><span class="sub">${esc((perf.ok && perf.value.symbol) || "latest backtest")}</span></div>
          ${perfCard}
        </div>
        <div class="card">
          <div class="card-head"><h2>Allocation</h2><span class="sub">by market value</span></div>
          ${alloc}
          ${acctLocked ? "" : `<div class="kv" style="margin-top:10px"><span class="k">Exposure</span><span class="v">${available ? pctPts(s.exposure_pct) + " of equity" : "—"}</span></div>
          <div class="kv"><span class="k">Positions</span><span class="v">${available ? num(s.positions, 0) : "—"}</span></div>`}
        </div>
      </section>
      <section class="grid two">
        <div class="card">
          <div class="card-head"><h2>Watchlist</h2><span class="sub">${esc((cfg.ok && cfg.value.n_symbols) || "")} symbols</span></div>
          ${quoteTable}
        </div>
        <div class="stack">
          <div class="card">
            <div class="card-head"><h2>System health</h2><span class="sub">${esc((health.ok && health.value.ts) ? stamp(health.value.ts) : "offline")}</span></div>
            ${health.ok ? healthRows(health.value) : errorBox("Health", health.error)}
            <div class="kv" style="margin-top:8px"><span class="k">Durable store</span><span class="v ${dbOk ? "ok" : "bad"}">${!db.ok ? "unavailable" : dbv.configured ? (dbv.ok ? `schema v${dbv.schema_version} · ${dbv.counts ? dbv.counts.orders + dbv.counts.signals + dbv.counts.backtests : 0} rows` : "verification failed") : "not configured"}</span></div>
          </div>
          <div class="card">
            <div class="card-head"><h2>Automation</h2><span class="pill ${autoV && autoV.running ? "ok" : "mut"}">${autoV && autoV.running ? "running" : "off"}</span></div>
            ${autoV ? `
              <div class="kv"><span class="k">Cycle</span><span class="v">${num(autoV.minutes, 0)} min</span></div>
              <div class="kv"><span class="k">Mode</span><span class="v">${autoV.dry_run ? "dry-run" : "paper"}</span></div>
              <div class="kv"><span class="k">Runs</span><span class="v">${num(autoV.runs, 0)}</span></div>
              <div class="kv"><span class="k">Orders</span><span class="v">${num(autoV.live_orders, 0)} live / ${num(autoV.dry_orders, 0)} dry</span></div>
              <p class="muted small" style="margin-top:8px">${esc(autoV.result || "No run yet.")}</p>`
            : errorBox("Automation", auto.error)}
            <div class="row" style="margin-top:10px"><a class="btn small" href="#" data-goto="risk">Risk controls</a></div>
          </div>
        </div>
      </section>
    </div>`;

    $$("[data-goto]", root).forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      state.view = a.dataset.goto;
      render();
    }));
  };

  VIEWS.portfolio = async (root, seq) => {
    root.innerHTML = `<div class="stack"><div class="skeleton" style="min-height:120px"></div><div class="skeleton" style="min-height:220px"></div></div>`;
    const [positions, orders, summary] = await Promise.all([
      gatedGet("/api/v2/positions"), gatedGet("/api/v2/orders"), gatedGet("/api/v2/summary"),
    ]);
    if (seq !== viewSeq) return;

    const acctLocked = authBlocked(positions, summary);
    const ordersLocked = authBlocked(orders);
    const p = positions.ok ? positions.value : null;
    const s = summary.ok ? summary.value : null;
    const avail = !!(p && p.available);
    const rows = (p && p.positions) || [];
    const totalMv = rows.reduce((a, x) => a + Math.abs(Number(x.market_value) || 0), 0);

    // The broker's own error text is rendered inside each panel (never a zero
    // balance or an empty book).
    const summaryStrip = acctLocked ? lockedPanel("Account metrics") : `<section class="grid kpis" aria-label="Account metrics">
      ${kpi("Equity", avail ? money(p.account.equity) : "—", avail ? (p.account.status || "broker-reported") : "unavailable", avail ? "ok" : "", !avail)}
      ${kpi("Cash", avail ? money(p.account.cash) : "—", "settled", "", !avail)}
      ${kpi("Buying power", avail ? money(p.account.bp) : "—", "available", "", !avail)}
      ${kpi("Positions value", avail ? money(totalMv) : "—", `${rows.length} holding(s)`, "", !avail)}
      ${kpi("Unrealized P&L", avail ? signedMoney(p.total_unrealized_pl) : "—", "mark-to-market", avail ? tone(p.total_unrealized_pl) : "", !avail)}
      ${kpi("Drawdown", avail && s && isNum(s.drawdown_pct) ? pctPts(-Math.abs(s.drawdown_pct)) : "—", "from peak", "", !avail)}
    </section>`;

    const positionsTable = positions.locked || positions.throttled
      ? lockedPanel("Positions")
      : !positions.ok
        ? errorBox("Positions", positions.error)
        : !avail
          ? brokerUnavailable("Positions", p.error)
          : rows.length
          ? `<div class="table-wrap"><table class="data">
              <caption class="sr-only">Open positions</caption>
              <thead><tr><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Market value</th><th class="num">Cost</th><th class="num">P&amp;L</th><th class="num">P&amp;L %</th><th class="num">Alloc</th></tr></thead>
              <tbody>${rows.map((x) => {
                const up = Number(x.unrealized_pl);
                return `<tr>
                  <td class="sym">${esc(x.symbol)}</td><td>${esc(x.side)}</td>
                  <td class="num">${qty(Number(x.qty))}</td>
                  <td class="num">${money(Number(x.current_price), 4)}</td>
                  <td class="num">${money(Number(x.market_value))}</td>
                  <td class="num">${money(Number(x.cost_basis))}</td>
                  <td class="num ${cmpClass(up)}">${signedMoney(up)}</td>
                  <td class="num ${cmpClass(up)}">${pctPts(Number(x.unrealized_pl_pct))}</td>
                  <td class="num">${totalMv > 0 ? ((Math.abs(Number(x.market_value) || 0) / totalMv) * 100).toFixed(1) + "%" : "—"}</td>
                </tr>`;
              }).join("")}</tbody></table></div>`
          : `<div class="empty">Flat — the paper account holds no positions.</div>`;

    const ordersV = orders.ok ? orders.value : null;
    const openOrders = (ordersV && ordersV.open) || [];
    const dbOpen = (ordersV && ordersV.db_open) || [];
    const hist = (ordersV && ordersV.history) || [];
    const brokerOrdersOk = !!(ordersV && ordersV.broker_available !== false);
    const ordersSub = ordersLocked
      ? "locked"
      : brokerOrdersOk ? `broker ${openOrders.length} · durable store ${dbOpen.length}` : `broker — · durable store ${dbOpen.length}`;

    const ordersBody = ordersLocked
      ? lockedPanel("Open orders")
      : !orders.ok
        ? errorBox("Orders", orders.error)
        : `<div class="stack">
            ${brokerOrdersOk ? "" : brokerUnavailable("Broker open orders", ordersV.open_error)}
            ${(openOrders.length || dbOpen.length) ? `
              <div class="table-wrap"><table class="data">
                <caption class="sr-only">Open orders</caption>
                <thead><tr><th>Symbol</th><th>Side</th><th class="num">Notional</th><th class="num">Qty</th><th>Status</th><th>Client order ID</th><th>Source</th></tr></thead>
                <tbody>
                  ${openOrders.map((o) => `<tr><td class="sym">${esc(o.symbol)}</td><td>${esc(o.side)}</td><td class="num">${money(Number(o.notional || 0))}</td><td class="num">${qty(Number(o.qty || 0))}</td><td>${esc(o.status)}</td><td class="mono small">${esc(o.client_order_id || "—")}</td><td class="muted">broker</td></tr>`).join("")}
                  ${dbOpen.filter((o) => !openOrders.some((b) => b.client_order_id === o.client_order_id)).map((o) => `<tr><td class="sym">${esc(o.symbol)}</td><td>${esc(o.side)}</td><td class="num">${money(Number(o.notional || 0))}</td><td class="num">—</td><td>${esc(o.status)}</td><td class="mono small">${esc(o.client_order_id || "—")}</td><td class="muted">store</td></tr>`).join("")}
                </tbody></table></div>`
              : brokerOrdersOk ? `<div class="empty">No open orders.</div>` : ""}
            ${ordersV.open_error && brokerOrdersOk ? `<p class="banner warn">${esc(ordersV.open_error)}</p>` : ""}
            ${ordersV.db_error ? `<p class="banner warn">${esc(ordersV.db_error)}</p>` : ""}
          </div>`;

    const historyBody = ordersLocked
      ? lockedPanel("Execution history")
      : hist.length ? `<div class="table-wrap"><table class="data">
          <caption class="sr-only">Execution history</caption>
          <thead><tr><th>Submitted</th><th>Symbol</th><th>Side</th><th class="num">Filled qty</th><th class="num">Avg price</th><th>Status</th><th>Client order ID</th><th>Note</th></tr></thead>
          <tbody>${hist.slice().reverse().map((x) => `<tr>
            <td class="mono small">${esc(shortTime(x.ts))}</td><td class="sym">${esc(x.symbol || "—")}</td><td>${esc(x.side || "—")}</td>
            <td class="num">${qty(Number(x.filled_qty || 0))}</td><td class="num">${x.filled_price ? money(Number(x.filled_price), 4) : "—"}</td>
            <td class="${x.status === "filled" ? "ok" : x.status === "rejected" || x.status === "unknown" ? "bad" : ""}">${esc(x.status || "—")}</td>
            <td class="mono small">${esc(x.cid || "—")}</td><td class="muted small">${esc(x.note || "")}</td>
          </tr>`).join("")}</tbody></table></div>` : `<div class="empty">No executions recorded in this process. Durable order records appear above.</div>`;

    root.innerHTML = `<div class="stack">
      ${summaryStrip}
      <section class="grid two">
        <div class="card">
          <div class="card-head"><h2>Holdings</h2><span class="sub">live paper account</span></div>
          ${positionsTable}
        </div>
        <div class="card">
          <div class="card-head"><h2>Allocation</h2><span class="sub">market value</span></div>
          ${acctLocked
            ? lockedPanel("Allocation")
            : !positions.ok
              ? errorBox("Positions", positions.error)
              : p && !p.available
                ? brokerUnavailable("Allocation", p.error)
                : rows.length ? allocationBlock(rows.map((x) => ({
                  symbol: x.symbol,
                  pct: totalMv > 0 ? (Math.abs(Number(x.market_value) || 0) / totalMv) * 100 : 0,
                })).sort((a, b) => b.pct - a.pct)) : `<div class="empty">No allocation to display.</div>`}
        </div>
      </section>
      <section class="card">
        <div class="card-head"><h2>Open orders</h2><span class="sub">${esc(ordersSub)}</span></div>
        ${ordersBody}
      </section>
      <section class="card">
        <div class="card-head"><h2>Execution history</h2><span class="sub">this process · ${ordersLocked ? "—" : hist.length}</span></div>
        ${historyBody}
      </section>
    </div>`;
  };

  VIEWS.markets = async (root, seq) => {
    root.innerHTML = `<div class="stack"><div class="skeleton" style="min-height:90px"></div><div class="skeleton" style="min-height:260px"></div></div>`;
    const [cfg, quotes] = await Promise.all([
      safeGet("/api/config/validate"), safeGet("/api/v2/quotes"),
    ]);
    if (seq !== viewSeq) return;
    const c = cfg.ok ? cfg.value : (state.config || {});
    const symbols = (c.symbols || ["BTC/USD"]).slice(0, 40);
    const timeframes = ["1Min", "5Min", "15Min", "1H", "1D"];
    const quoteMap = new Map();
    if (quotes.ok) quotes.value.quotes.forEach((q) => quoteMap.set(q.symbol, q));

    root.innerHTML = `<div class="stack">
      <div class="card">
        <div class="card-head"><h2>Market watch</h2><span class="sub">${quotes.ok ? esc(stamp(quotes.value.as_of)) : "offline"}</span></div>
        ${quotes.ok ? `<div class="table-wrap"><table class="data">
          <caption class="sr-only">Watchlist quotes</caption>
          <thead><tr><th>Symbol</th><th class="num">Last</th><th class="num">Change</th><th class="num">%</th><th>Quality</th></tr></thead>
          <tbody>${quotes.value.quotes.map(quoteRow).join("")}</tbody></table></div>
          ${quotes.value.available ? "" : `<p class="banner warn" style="margin-top:8px">Market data provider is not returning bars. Configure paper market-data credentials or DATA_PROVIDER.</p>`}`
          : errorBox("Watchlist", quotes.error)}
      </div>
      <div class="card">
        <div class="card-head"><h2>Candlestick chart</h2><span class="sub" id="chartMeta">—</span></div>
        <div class="toolbar" style="margin-bottom:12px">
          <label class="field"><span>Symbol</span><select id="mSymbol">${symbols.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}</select></label>
          <label class="field"><span>Timeframe</span><select id="mTf">${timeframes.map((t) => `<option value="${t}" ${t === c.timeframe ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>
          <label class="field"><span>Bars</span><select id="mLimit">${[60, 120, 240, 480].map((n) => `<option value="${n}" ${n === 120 ? "selected" : ""}>${n}</option>`).join("")}</select></label>
          <button class="btn primary" id="mLoad" type="button">Load chart</button>
        </div>
        <div id="mQuote" class="row" style="margin-bottom:10px"></div>
        <div id="mChart">${emptyChart("Choose a symbol and load the chart.", 260)}</div>
        <div id="mQuality" class="muted small" style="margin-top:8px"></div>
      </div>
    </div>`;

    const load = async () => {
      const sym = $("#mSymbol").value;
      const tf = $("#mTf").value;
      const limit = Number($("#mLimit").value);
      $("#chartMeta").textContent = "loading…";
      $("#mChart").innerHTML = `<div class="skeleton" style="min-height:260px"></div>`;
      try {
        const d = await apiGet(`/api/market/bars?symbol=${encodeURIComponent(sym)}&timeframe=${encodeURIComponent(tf)}&limit=${limit}`, { ttl: 0 });
        if (seq !== viewSeq) return;
        const last = d.bars[d.bars.length - 1];
        const q = quoteMap.get(sym);
        $("#chartMeta").textContent = `${d.symbol} · ${d.timeframe} · ${d.bars.length} bars`;
        $("#mQuote").innerHTML = `
          <span class="pill mut">${esc(d.symbol)}</span>
          <strong class="mono">${last ? money(Number(last.c), Number(last.c) < 10 ? 4 : 2) : "—"}</strong>
          ${q && q.available ? `<span class="${cmpClass(q.change)} mono">${signedMoney(q.change, 4)} / ${pctPts(q.change_pct)}</span>` : `<span class="muted small">quote unavailable</span>`}`;
        $("#mChart").innerHTML = candleChart(d.bars, `Candlestick chart for ${d.symbol} ${d.timeframe}`);
        const quality = d.quality || {};
        $("#mQuality").innerHTML = quality.ok
          ? `<span class="pill ok">fresh</span> ${num(quality.rows, 0)} rows · last bar ${timeAgo(Number(last && last.ts))}`
          : `<span class="pill warn">quality</span> ${esc((quality.issues || []).join("; "))}`;
      } catch (e) {
        recordError("/api/market/bars", e);
        if (seq !== viewSeq) return;
        $("#chartMeta").textContent = "error";
        $("#mChart").innerHTML = errorBox("Market data", e);
        $("#mQuality").textContent = e.message;
      }
    };
    $("#mLoad").addEventListener("click", load);
    $("#mSymbol").addEventListener("change", load);
    $("#mTf").addEventListener("change", load);
    $("#mLimit").addEventListener("change", load);
    const preferred = symbols.includes("BTC/USD") ? "BTC/USD" : symbols[0];
    if (preferred) { $("#mSymbol").value = preferred; load(); }
  };

  VIEWS.strategies = async (root, seq) => {
    root.innerHTML = `<div class="stack"><div class="skeleton" style="min-height:160px"></div><div class="skeleton" style="min-height:220px"></div></div>`;
    const [signals, muse] = await Promise.all([gatedGet("/api/v2/signals"), safeGet("/api/v2/muse")]);
    if (seq !== viewSeq) return;
    const signalsLocked = authBlocked(signals);
    const sigs = signals.ok ? (signals.value.signals || {}) : {};
    const keys = Object.keys(sigs);
    const mu = muse.ok ? muse.value : null;

    const actionClass = (a) => a === "BUY" ? "ok" : a === "SELL" ? "bad" : "mut";
    const cards = keys.map((sym) => {
      const v = sigs[sym] || {};
      const a = v.aggregate || { action: "ABSTAIN", score: 0, confidence: 0, reason: "no data" };
      const rows = v.signals || [];
      return `<div class="card">
        <div class="card-head">
          <h2>${esc(sym)}</h2>
          <span class="pill ${actionClass(a.action)}">${esc(a.action)}</span>
        </div>
        <div class="metric-list">
          <div class="metric"><div class="k">Score</div><div class="v">${num(a.score, 3)}</div></div>
          <div class="metric"><div class="k">Confidence</div><div class="v">${num(a.confidence, 3)}</div></div>
          <div class="metric"><div class="k">Disagreement</div><div class="v">${num(a.disagree, 3)}</div></div>
        </div>
        <p class="muted small" style="margin:8px 0">${esc(a.reason || "")}</p>
        ${rows.length ? `<div class="table-wrap"><table class="data">
          <caption class="sr-only">Strategy contributions for ${esc(sym)}</caption>
          <thead><tr><th>Strategy</th><th class="num">Signal</th><th class="num">Confidence</th><th>Reason</th></tr></thead>
          <tbody>${rows.map((x) => `<tr><td>${esc(x.name || "—")}</td><td class="num ${x.signal > 0 ? "ok" : x.signal < 0 ? "bad" : ""}">${num(x.signal, 3)}</td><td class="num">${num(x.conf, 3)}</td><td class="muted small">${esc(x.reason || "")}</td></tr>`).join("")}</tbody>
        </table></div>` : ""}
      </div>`;
    }).join("");

    root.innerHTML = `<div class="stack">
      <div class="card">
        <div class="card-head"><h2>Muse AI</h2><span class="pill ${mu && mu.available ? "ok" : "mut"}">${mu && mu.available ? "available" : "fail-closed"}</span></div>
        ${mu ? `
          <div class="kv"><span class="k">Configured</span><span class="v">${mu.enabled ? "yes" : "no"}</span></div>
          <div class="kv"><span class="k">Model</span><span class="v">${esc(mu.model || "—")}</span></div>
          <div class="kv"><span class="k">Failures</span><span class="v">${num(mu.failures, 0)}</span></div>
          <div class="kv"><span class="k">Cooling down</span><span class="v">${mu.cooling_down ? "yes" : "no"}</span></div>
          <div class="kv"><span class="k">Last error</span><span class="v">${esc(mu.last_error || "—")}</span></div>
          <p class="muted small" style="margin-top:8px">Decision support only. Unavailable reviews abstain; they never place orders.</p>`
        : errorBox("Muse status", muse.error)}
      </div>
      <div class="row between">
        <h2 class="section-title" style="margin:0">Signals (${signalsLocked ? "—" : keys.length})</h2>
        <button class="btn primary" id="runPipeline" type="button">Run pipeline</button>
      </div>
      ${signalsLocked
        ? lockedPanel("Signals")
        : !signals.ok
          ? errorBox("Signals", signals.error)
          : keys.length
            ? `<div class="grid two-even">${cards}</div>`
            : `<div class="empty"><strong>No signals yet</strong><span>Run the pipeline once market data is available.</span></div>`}
    </div>`;

    $("#runPipeline").addEventListener("click", async (e) => {
      if (!ensureOps("the signal pipeline")) return;
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Running…";
      try {
        await apiPost("/api/pipeline/run", { password: state.pwd });
        toast("Pipeline completed.", "ok");
        clearCache();
        render();
      } catch (err) {
        recordError("/api/pipeline/run", err);
        toast(err.message, "bad");
        btn.disabled = false;
        btn.textContent = "Run pipeline";
      }
    });
  };

  VIEWS.backtest = async (root, seq) => {
    root.innerHTML = `<div class="stack"><div class="skeleton" style="min-height:110px"></div><div class="skeleton" style="min-height:260px"></div></div>`;
    const [cfg, perf] = await Promise.all([safeGet("/api/config/validate"), gatedGet("/api/v2/performance")]);
    if (seq !== viewSeq) return;
    const perfLocked = authBlocked(perf);
    const c = cfg.ok ? cfg.value : (state.config || {});
    const symbols = (c.symbols || ["BTC/USD"]).slice(0, 40);
    const timeframes = ["1Min", "5Min", "15Min", "1H", "1D"];

    root.innerHTML = `<div class="stack">
      <div class="card">
        <div class="card-head"><h2>Backtest</h2><span class="sub">fees + slippage charged · no look-ahead</span></div>
        <div class="toolbar">
          <label class="field"><span>Symbol</span><select id="bSymbol">${symbols.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}</select></label>
          <label class="field"><span>Timeframe</span><select id="bTf">${timeframes.map((t) => `<option value="${t}" ${t === c.timeframe ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>
          <label class="field"><span>Bars</span><input id="bLimit" type="number" min="60" max="2000" step="10" value="300"></label>
          <button class="btn primary" id="bRun" type="button">Run backtest</button>
          <button class="btn ghost" id="bCancel" type="button" disabled>Cancel</button>
          <button class="btn ghost" id="bWalk" type="button">Walk-forward windows</button>
        </div>
        <div id="bStatus" class="muted small" style="margin-top:10px">${!perfLocked && perf.ok && perf.value.source === "none" ? esc(perf.value.message) : ""}</div>
        <div class="progress" style="margin-top:8px"><span id="bProgress" style="width:0%"></span></div>
      </div>
      <div id="bResult"></div>
    </div>`;

    const renderResult = (p) => {
      if (!p || p.source === "none") {
        $("#bResult").innerHTML = `<div class="empty"><strong>No completed backtest</strong><span>Run one to see equity, drawdown and benchmark curves.</span></div>`;
        return;
      }
      const curve = p.curve || [];
      const bench = p.benchmark || [];
      const dd = drawdownSeries(curve);
      const m = p.metrics || {};
      const trades = (p.trades || []).slice(-25).reverse();
      $("#bResult").innerHTML = `<div class="stack">
        <section class="grid kpis">
          ${kpi("Total return", pctFrac(m.total_return), `${esc(p.symbol || "")} · ${esc(p.assumptions && p.assumptions.exec || "")}`, tone(m.total_return))}
          ${kpi("Sharpe", num(m.sharpe, 2), "per-bar, annualisation not assumed")}
          ${kpi("Max drawdown", pctFrac(-Math.abs(m.max_dd || 0)), "peak to trough", "warn")}
          ${kpi("Trades", num(m.trades, 0), "rebalances", "")}
          ${kpi("Fees + slippage", money(m.fees), "charged", "")}
          ${kpi("Final equity", money(m.equity), "starting $10,000", "")}
        </section>
        <section class="grid two">
          <div class="card">
            <div class="card-head"><h2>Equity vs benchmark</h2><span class="sub">${p.benchmark_return !== undefined ? "B&H " + pctFrac(p.benchmark_return) : ""}</span></div>
            <div class="chart-wrap">${lineChart({
              series: [
                { values: curve, color: "var(--accent)", fill: true },
                { values: bench, color: "var(--faint)", dash: true },
              ],
              yFormat: (v) => money(v, 0),
              ariaLabel: "Backtest equity curve versus buy and hold",
            })}</div>
            <div class="legend"><span><i style="background:var(--accent)"></i>Strategy</span><span><i style="background:var(--faint)"></i>Buy &amp; hold</span></div>
          </div>
          <div class="card">
            <div class="card-head"><h2>Drawdown</h2><span class="sub">% from peak</span></div>
            <div class="chart-wrap">${lineChart({
              series: [{ values: dd, color: "var(--bad)", fill: true }],
              yFormat: (v) => `${v.toFixed(1)}%`,
              ariaLabel: "Backtest drawdown",
            })}</div>
          </div>
        </section>
        <section class="card">
          <div class="card-head"><h2>Recent trades</h2><span class="sub">last ${trades.length}</span></div>
          ${trades.length ? `<div class="table-wrap"><table class="data">
            <caption class="sr-only">Backtest trades</caption>
            <thead><tr><th class="num">Bar</th><th class="num">Price</th><th class="num">Notional change</th><th class="num">Cost</th></tr></thead>
            <tbody>${trades.map((t) => `<tr><td class="num">${num(t.bar, 0)}</td><td class="num">${money(t.price, 4)}</td><td class="num ${t.dnot > 0 ? "ok" : "bad"}">${signedMoney(t.dnot)}</td><td class="num">${money(t.fee, 4)}</td></tr>`).join("")}</tbody>
          </table></div>` : `<div class="empty">No trades in this backtest.</div>`}
          <p class="muted small" style="margin-top:8px">${esc((p.assumptions && p.assumptions.note) || "Past performance is not evidence of future profit.")}</p>
        </section>
      </div>`;
    };

    if (perfLocked) $("#bResult").innerHTML = lockedPanel("Backtest results");
    else renderResult(perf.ok ? perf.value : null);

    const pollJob = async (id) => {
      for (let i = 0; i < 90; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        if (seq !== viewSeq) return;
        try {
          // Job status is account data: authenticated header read, no retry on
          // 403/429 (the loop below stops on any error).
          const j = await apiGet(`/api/jobs/${id}`, { ttl: 0, auth: true });
          const pct = j.status === "done" ? 100 : j.status === "running" ? 60 : 15;
          $("#bProgress").style.width = pct + "%";
          $("#bStatus").textContent = `job ${id} · ${j.status}${j.error ? " · " + j.error : ""}`;
          if (j.status === "done") {
            $("#bProgress").style.width = "100%";
            $("#bStatus").textContent = `job ${id} · done`;
            renderResult({ source: "latest_backtest_job", ...(j.result || {}) });
            toast("Backtest complete.", "ok");
            $("#bRun").disabled = false;
            $("#bCancel").disabled = true;
            return;
          }
          if (j.status === "failed" || j.status === "canceled") {
            $("#bStatus").textContent = `job ${id} · ${j.status}${j.error ? ": " + j.error : ""}`;
            $("#bRun").disabled = false;
            $("#bCancel").disabled = true;
            return;
          }
        } catch (e) {
          if (authError(e)) noteAuthFailure(e);
          recordError(`/api/jobs/${id}`, e);
          $("#bStatus").textContent = `job status unavailable: ${e.message}`;
          $("#bRun").disabled = false;
          $("#bCancel").disabled = true;
          return;
        }
      }
      $("#bStatus").textContent = "Job is still running. Refresh the page to resume polling.";
      $("#bRun").disabled = false;
      $("#bCancel").disabled = true;
    };

    $("#bRun").addEventListener("click", async (e) => {
      if (seq !== viewSeq) return;
      if (!ensureOps("backtests")) return;
      const symbol = $("#bSymbol").value;
      const timeframe = $("#bTf").value;
      const limit = Math.max(60, Math.min(2000, Number($("#bLimit").value) || 300));
      const btn = e.currentTarget;
      btn.disabled = true;
      $("#bCancel").disabled = false;
      $("#bProgress").style.width = "10%";
      $("#bStatus").textContent = "submitting…";
      try {
        const r = await apiPost("/api/backtest", { symbol, timeframe, limit, password: state.pwd });
        state.lastJob = r.job_id;
        $("#bStatus").textContent = `job ${r.job_id} · ${r.status}`;
        pollJob(r.job_id);
      } catch (err) {
        recordError("/api/backtest", err);
        $("#bStatus").textContent = err.message;
        $("#bProgress").style.width = "0%";
        btn.disabled = false;
        $("#bCancel").disabled = true;
        toast(err.message, "bad");
      }
    });

    $("#bCancel").addEventListener("click", async () => {
      if (!state.lastJob) return;
      if (!ensureOps("job cancel")) return;
      try {
        await apiPost(`/api/jobs/${state.lastJob}/cancel`, { password: state.pwd });
        $("#bStatus").textContent = `job ${state.lastJob} · cancel requested`;
      } catch (err) {
        if (authError(err)) noteAuthFailure(err);
        toast(err.message, "bad");
      }
    });

    $("#bWalk").addEventListener("click", async () => {
      if (!ensureOps("walk-forward windows")) return;
      try {
        const r = await apiPost("/api/walkforward?n=1000&tr=400&va=200&te=200&step=200", { password: state.pwd });
        const wins = r.windows || [];
        $("#bResult").innerHTML = `<div class="card"><div class="card-head"><h2>Walk-forward windows</h2><span class="sub">${wins.length} windows</span></div>
          ${wins.length ? `<div class="table-wrap"><table class="data"><caption class="sr-only">Walk-forward windows</caption>
          <thead><tr><th class="num">#</th><th>Train</th><th>Validate</th><th>Test</th></tr></thead>
          <tbody>${wins.map((w, i) => `<tr><td class="num">${i + 1}</td><td class="mono">${esc(JSON.stringify(w.train))}</td><td class="mono">${esc(JSON.stringify(w.val))}</td><td class="mono">${esc(JSON.stringify(w.test))}</td></tr>`).join("")}</tbody>
          </table></div>` : `<div class="empty">Not enough bars for these windows.</div>`}</div>`;
      } catch (err) {
        toast(err.message, "bad");
      }
    });
  };

  VIEWS.risk = async (root, seq) => {
    root.innerHTML = `<div class="stack"><div class="skeleton" style="min-height:160px"></div><div class="skeleton" style="min-height:220px"></div></div>`;
    const [risk, positions, auto] = await Promise.all([
      safeGet("/api/v2/risk"), gatedGet("/api/v2/positions"), safeGet("/api/v2/automate/status"),
    ]);
    if (seq !== viewSeq) return;
    const acctLocked = authBlocked(positions);
    const r = risk.ok ? risk.value : null;
    const p = positions.ok ? positions.value : null;
    const a = auto.ok ? auto.value : null;
    const brokerDown = !acctLocked && !!p && !p.available;
    const rows = (p && p.positions) || [];
    const equity = p && p.account ? Number(p.account.equity) : null;
    const exposure = rows.reduce((sum, x) => sum + Math.abs(Number(x.market_value) || 0), 0);
    const exposurePct = isNum(equity) && equity > 0 ? (exposure / equity) * 100 : null;

    const exposureBody = acctLocked
      ? lockedPanel("Exposure")
      : brokerDown
        ? brokerUnavailable("Exposure", p.error)
        : `<div class="kv"><span class="k">Gross exposure</span><span class="v">${p && p.available ? money(exposure) : "—"}</span></div>
          <div class="kv"><span class="k">Equity</span><span class="v">${p && p.available ? money(equity) : "—"}</span></div>
          <div class="kv"><span class="k">Exposure / equity</span><span class="v ${exposurePct && exposurePct > 100 ? "warn" : ""}">${p && p.available ? pctPts(exposurePct) : "—"}</span></div>
          <div class="kv"><span class="k">Session day P&L</span><span class="v ${r ? tone(r.daily_loss) : ""}">${r ? signedMoney(r.daily_loss) : "—"}</span></div>`;

    const concentrationBody = acctLocked
      ? lockedPanel("Concentration")
      : brokerDown
        ? brokerUnavailable("Concentration", p.error)
        : rows.length ? `<div class="table-wrap"><table class="data">
            <caption class="sr-only">Position concentration</caption>
            <thead><tr><th>Symbol</th><th class="num">Market value</th><th class="num">% equity</th></tr></thead>
            <tbody>${rows.map((x) => {
              const pctv = isNum(equity) && equity > 0 ? (Math.abs(Number(x.market_value) || 0) / equity) * 100 : null;
              return `<tr><td class="sym">${esc(x.symbol)}</td><td class="num">${money(Number(x.market_value))}</td><td class="num ${pctv && pctv > 50 ? "warn" : ""}">${pctH(pctv)}</td></tr>`;
            }).join("")}</tbody></table></div>` : `<div class="empty">No positions.</div>`;

    const limitRows = r ? Object.entries(r.limits || {}).map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${isNum(v) ? num(v, 2) : esc(String(v))}</span></div>`).join("") : "";

    root.innerHTML = `<div class="stack">
      ${r && r.killed ? banner("bad", "Kill switch engaged. All new orders are blocked until the process restarts.") : banner("ok", "Kill switch armed. Every order is re-checked against all limits before submission.")}
      ${state.config && state.config.password_required === false ? banner("warn", "Trading controls are locked: set DASHBOARD_PASSWORD on the server and restart to enable orders, the kill switch and automation.") : ""}
      <section class="grid three">
        <div class="card">
          <div class="card-head"><h2>Exposure</h2><span class="sub">paper account</span></div>
          ${exposureBody}
        </div>
        <div class="card">
          <div class="card-head"><h2>Limits</h2><span class="sub">enforced server-side</span></div>
          ${r ? limitRows : errorBox("Risk limits", risk.error)}
        </div>
        <div class="card">
          <div class="card-head"><h2>Automation</h2><span class="pill ${a && a.running ? "ok" : "mut"}">${a && a.running ? "running" : "off"}</span></div>
          ${a ? `
            <div class="kv"><span class="k">Dry run</span><span class="v">${a.dry_run ? "yes" : "no"}</span></div>
            <div class="kv"><span class="k">Interval</span><span class="v">${num(a.minutes, 0)} min</span></div>
            <div class="kv"><span class="k">Runs</span><span class="v">${num(a.runs, 0)}</span></div>
            <div class="kv"><span class="k">Live / dry orders</span><span class="v">${num(a.live_orders, 0)} / ${num(a.dry_orders, 0)}</span></div>
            <div class="row" style="margin-top:10px">
              <button class="btn small" id="autoToggle" type="button">${a.running ? "Stop" : "Start"}</button>
              <button class="btn small ghost" id="autoOnce" type="button">Run once</button>
            </div>` : errorBox("Automation", auto.error)}
        </div>
      </section>
      <section class="grid two">
        <div class="card">
          <div class="card-head"><h2>Concentration</h2><span class="sub">by symbol</span></div>
          ${concentrationBody}
        </div>
        <div class="card">
          <div class="card-head"><h2>Emergency stop</h2><span class="sub">blocks new orders</span></div>
          <p class="muted small">The kill switch is process-local. It blocks every new submission but does not liquidate existing positions; review them in the Portfolio tab.</p>
          <div class="row" style="margin-top:10px">
            <button class="btn danger" id="killBtn" type="button" ${r && r.killed ? "disabled" : ""}>Engage kill switch</button>
            <button class="btn ghost" id="unlockBtn" type="button">${state.ops ? "Controls unlocked" : "Unlock controls"}</button>
          </div>
        </div>
      </section>
    </div>`;

    function pctH(v) { return isNum(v) ? v.toFixed(1) + "%" : "—"; }

    const signal = (msg) => { $("#killBtn").insertAdjacentHTML("afterend", `<span class="muted small">${esc(msg)}</span>`); };

    $("#unlockBtn").addEventListener("click", () => { if (!state.ops) openAuth(); });

    $("#killBtn").addEventListener("click", async () => {
      if (!ensureOps("the kill switch")) return;
      if (!window.confirm("Engage the kill switch? All new orders will be blocked immediately.")) return;
      try {
        await apiPost("/api/kill", { password: state.pwd, reason: "dashboard manual" });
        toast("Kill switch engaged.", "bad");
        clearCache();
        render();
      } catch (e) {
        recordError("/api/kill", e);
        toast(e.message, "bad");
      }
    });

    const autoToggle = $("#autoToggle");
    if (autoToggle) autoToggle.addEventListener("click", async () => {
      if (!ensureOps("automation")) return;
      const action = a && a.running ? "stop" : "start";
      if (action === "start" && !window.confirm("Start the paper automation loop? It will place paper orders on its interval.")) return;
      autoToggle.disabled = true;
      try {
        await apiPost(`/api/automate/${action}`, { password: state.pwd });
        toast(`Automation ${action === "start" ? "started" : "stopped"}.`, "ok");
        clearCache();
        render();
      } catch (e) {
        recordError(`/api/automate/${action}`, e);
        toast(e.message, "bad");
        autoToggle.disabled = false;
      }
    });

    const autoOnce = $("#autoOnce");
    if (autoOnce) autoOnce.addEventListener("click", async () => {
      if (!ensureOps()) return;
      autoOnce.disabled = true;
      try {
        const res = await apiPost("/api/automate/run-once", { password: state.pwd });
        toast(`Automation cycle: ${res.result || "done"}`, "ok");
        clearCache();
        render();
      } catch (e) {
        recordError("/api/automate/run-once", e);
        toast(e.message, "bad");
        autoOnce.disabled = false;
      }
    });
  };

  VIEWS.activity = async (root, seq) => {
    root.innerHTML = `<div class="stack"><div class="skeleton" style="min-height:220px"></div></div>`;
    const [logs] = await Promise.all([gatedGet("/api/logs?limit=300", { ttl: 0 })]);
    if (seq !== viewSeq) return;
    const logsLocked = authBlocked(logs);
    const events = logs.ok ? (logs.value.events || []).slice().reverse() : [];
    const bad = events.filter((e) => e.level === "error" || e.level === "critical");
    const warn = events.filter((e) => e.level === "warning");

    root.innerHTML = `<div class="stack">
      <div class="card">
        <div class="card-head"><h2>Server events</h2><span class="sub">${logsLocked ? "locked" : `${events.length} recent · ${bad.length} errors · ${warn.length} warnings`}</span></div>
        ${logsLocked
          ? lockedPanel("Server events")
          : logs.ok ? (events.length ? `<div class="table-wrap"><table class="data">
          <caption class="sr-only">Server log events</caption>
          <thead><tr><th>Time</th><th>Level</th><th>Event</th><th>Detail</th></tr></thead>
          <tbody>${events.map((e) => `<tr>
            <td class="mono small">${esc(String(e.ts || "").slice(0, 19))}</td>
            <td class="${e.level === "error" || e.level === "critical" ? "bad" : e.level === "warning" ? "warn" : "muted"}">${esc(e.level || "")}</td>
            <td>${esc(e.event || "")}</td>
            <td class="muted small">${esc(JSON.stringify(e.reason || e.error || e.path || e.detail || "").slice(0, 240))}</td>
          </tr>`).join("")}</tbody></table></div>` : `<div class="empty">No server events recorded.</div>`) : errorBox("Server logs", logs.error)}
      </div>
      <div class="card">
        <div class="card-head"><h2>Browser request errors</h2><span class="sub">${state.errors.length} this tab</span></div>
        ${state.errors.length ? `<div class="table-wrap"><table class="data">
          <caption class="sr-only">Browser request errors</caption>
          <thead><tr><th>Time</th><th>Request</th><th>Error</th></tr></thead>
          <tbody>${state.errors.slice().reverse().map((e) => `<tr><td class="mono small">${esc(shortTime(new Date(e.ts).toISOString()))}</td><td class="mono small">${esc(e.where)}</td><td class="bad small">${esc(e.msg)}</td></tr>`).join("")}</tbody>
        </table></div>` : `<div class="empty">No failed requests in this tab.</div>`}
      </div>
    </div>`;
  };

  VIEWS.settings = async (root, seq) => {
    root.innerHTML = `<div class="stack"><div class="skeleton" style="min-height:220px"></div></div>`;
    const [cfg, health, db] = await Promise.all([
      safeGet("/api/config/validate"), safeGet("/api/health"), safeGet("/api/v2/db"),
    ]);
    if (seq !== viewSeq) return;
    const c = cfg.ok ? cfg.value : null;
    const h = health.ok ? health.value : null;
    const d = db.ok ? db.value : null;

    const configRows = c ? Object.entries({
      "Trading mode": c.trading_mode,
      "Live trading": c.live ? "ON (blocked)" : "OFF",
      "Paper orders": c.paper_orders ? "ON" : "OFF",
      "Symbols": `${c.n_symbols} configured`,
      "Timeframe": c.timeframe,
      "Data provider": c.data_provider,
      "Take profit": `${num(c.take_profit_pct, 2)}%`,
      "Fee / slippage": `${num(c.fee_pct * 100, 3)}% / ${num(c.slippage_pct * 100, 3)}%`,
      "Durable store required": c.durable_store_required ? "yes" : "no",
      "Dashboard password": c.password_required ? "required" : "not set (trading controls locked)",
    }).map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`).join("") : "";

    root.innerHTML = `<div class="stack">
      <section class="grid two">
        <div class="card">
          <div class="card-head"><h2>Safe configuration</h2><span class="sub">secrets are never returned</span></div>
          ${c ? configRows : errorBox("Configuration", cfg.error)}
        </div>
        <div class="card">
          <div class="card-head"><h2>Connections</h2><span class="sub">health snapshot</span></div>
          ${h ? healthRows(h) : errorBox("Health", health.error)}
          <div class="kv" style="margin-top:8px"><span class="k">API base</span><span class="v mono small">${esc(API_BASE || "same origin")}</span></div>
          <div class="kv"><span class="k">Browser timezone</span><span class="v mono small">${esc(tzName())}</span></div>
          <div class="kv"><span class="k">Build</span><span class="v mono small">static · v8</span></div>
        </div>
      </section>
      <section class="grid two">
        <div class="card">
          <div class="card-head"><h2>Durable store</h2><span class="pill ${d && d.ok ? "ok" : "bad"}">${d && d.ok ? "verified" : (d && d.require_durable ? "required · unavailable" : "not verified")}</span></div>
          ${d ? `
            <div class="kv"><span class="k">Backend</span><span class="v">${esc(d.backend || "—")}</span></div>
            <div class="kv"><span class="k">Connection</span><span class="v">${d.connected ? "connected" : (d.configured ? "disconnected" : "not configured")}</span></div>
            <div class="kv"><span class="k">Durable writes</span><span class="v">${d.durable ? "yes" : "no"}</span></div>
            <div class="kv"><span class="k">Write path</span><span class="v">${d.fallback_memory ? "in-memory fallback" : "PostgreSQL only"}</span></div>
            <div class="kv"><span class="k">Configured</span><span class="v">${d.configured ? "yes" : "no"}</span></div>
            <div class="kv"><span class="k">Schema version</span><span class="v">${esc(d.schema_version ?? "—")} / ${esc(d.expected_version ?? "—")}</span></div>
            <div class="kv"><span class="k">Signals</span><span class="v">${d.counts ? num(d.counts.signals, 0) : "—"}</span></div>
            <div class="kv"><span class="k">Backtests</span><span class="v">${d.counts ? num(d.counts.backtests, 0) : "—"}</span></div>
            <div class="kv"><span class="k">Orders</span><span class="v">${d.counts ? num(d.counts.orders, 0) : "—"}</span></div>
            ${d.missing_indexes && d.missing_indexes.length ? `<p class="banner bad" style="margin-top:8px">Missing indexes: ${esc(d.missing_indexes.join(", "))}</p>` : ""}
            ${d.error ? `<p class="banner bad" style="margin-top:8px">${esc(d.error)}</p>` : ""}`
          : errorBox("Database verification", db.error)}
        </div>
        <div class="card">
          <div class="card-head"><h2>Session &amp; appearance</h2><span class="sub">local only</span></div>
          <div class="row" style="margin-bottom:10px">
            <button class="btn" id="setTheme" type="button">Toggle theme</button>
            <button class="btn ghost" id="setLock" type="button">${state.ops ? "Lock controls" : "Unlock controls"}</button>
          </div>
          ${kvRow("Account data", state.pwd ? "unlocked for this tab" : passwordRequired() ? "locked" : "not required by the server")}
          ${kvRow("Password storage", "memory only — no localStorage, cookie or URL")}
          ${kvRow("Values", "broker-reported paper account; simulated only where labelled")}
          ${authNote() ? `<p class="banner warn" style="margin-top:8px">${esc(authNote())}</p>` : ""}
          <p class="muted small" style="margin-top:8px">The dashboard password is held in memory for this tab only. Locking clears it and drops cached account reads. Refreshing or closing the tab clears it too. It is never written to localStorage or the build output.</p>
          <p class="muted small" style="margin-top:8px">Paper trading only. Live trading is disabled in configuration and rejected at startup. Automation never starts on its own.</p>
        </div>
      </section>
    </div>`;

    $("#setTheme").addEventListener("click", () => setTheme(state.theme === "dark" ? "light" : "dark"));
    $("#setLock").addEventListener("click", () => {
      if (state.ops) lockControls();
      else openAuth();
    });
  };

  /* ---------------- render loop ---------------- */
  function lockControls() {
    state.ops = false;
    state.pwd = "";
    state.authState = passwordRequired() ? "locked" : "unknown";
    state.authNote = "";
    state.authToast = "";
    state.authCooldownUntil = 0;
    // Authenticated cache entries are dropped with the password so no account
    // payload can be served back to a locked session.
    clearCache({ authOnly: true });
    updateLockButton();
    toast("Locked. Account data is hidden until you unlock again.", "ok");
    render();
  }

  async function render({ silent = false } = {}) {
    const seq = ++viewSeq;
    clearTimeout(pollTimer);
    const tab = TABS.find((t) => t.id === state.view) || TABS[0];
    const nav = $("#nav");
    if (nav) {
      $$("button[data-tab]", nav).forEach((b) => {
        b.setAttribute("aria-current", b.dataset.tab === state.view ? "page" : "false");
      });
    }
    $("#pageTitle").textContent = tab.label;
    $("#pageSubtitle").textContent = tab.sub;
    document.title = `${tab.label} · KALA-LUND`;
    if (!silent) window.scrollTo(0, 0);
    try {
      await VIEWS[state.view]($("#view"), seq);
    } catch (e) {
      recordError(tab.label, e);
      if (seq === viewSeq) $("#view").innerHTML = `<div class="error-state"><strong>View failed to render</strong><span>${esc(e.message)}</span></div>`;
    }
    if (seq !== viewSeq) return;
    state.updatedAt = new Date().toISOString();
    updateChrome(state.health, state.config);
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    const interval = state.view === "overview" ? 20000 : 30000;
    pollTimer = setTimeout(async () => {
      if (document.visibilityState === "hidden") { schedulePoll(); return; }
      await refreshChrome();
      render({ silent: true });
    }, interval);
  }

  /* ---------------- init ---------------- */
  async function init() {
    setTheme(state.theme);
    const savedView = localStorage.getItem("kl-view");
    if (savedView && TABS.some((t) => t.id === savedView)) state.view = savedView;
    buildNav();
    updateLockButton();

    $("#themeToggle").addEventListener("click", () => setTheme(state.theme === "dark" ? "light" : "dark"));
    $("#btnRefresh").addEventListener("click", async () => {
      clearCache();
      await refreshChrome();
      render();
      toast("Refreshed", "ok");
    });
    $("#btnLock").addEventListener("click", () => {
      if (state.ops) lockControls();
      else if (state.config && state.config.password_required === false) {
        toast("Trading controls are locked: DASHBOARD_PASSWORD is not configured on the server.", "warn");
      } else openAuth();
    });

    // Every locked placeholder carries a real Unlock affordance; one delegated
    // listener keeps it working across re-renders without inline handlers (CSP).
    document.addEventListener("click", (e) => {
      const btn = e.target && e.target.closest ? e.target.closest("[data-unlock]") : null;
      if (!btn) return;
      e.preventDefault();
      if (state.config && state.config.password_required === false) {
        toast("DASHBOARD_PASSWORD is not configured on the server, so there is nothing to unlock.", "warn");
        return;
      }
      openAuth();
    });

    $("#authForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const pwd = $("#authPwd").value;
      const err = $("#authErr");
      err.hidden = true;
      try {
        await unlock(pwd);
        // Load account data immediately with the new credential instead of
        // waiting for the next poll. unlock() already dropped every cache
        // entry, so nothing here can serve a pre-unlock payload.
        await refreshChrome();
        render();
      } catch (ex) {
        recordError("/api/auth", ex);
        err.hidden = false;
        err.textContent = ex.status === 403 ? "Invalid password."
          : ex.status === 429 ? "Too many attempts. Wait a minute and try again." : ex.message;
      }
    });
    $("#authReadonly").addEventListener("click", () => {
      state.ops = false;
      state.pwd = "";
      state.authState = passwordRequired() ? "locked" : "unknown";
      state.authNote = "";
      updateLockButton();
      closeAuth();
      render();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("#authModal").hidden) closeAuth();
    });

    const cfg = await safeGet("/api/config/validate", { ttl: 0 });
    if (cfg.ok) state.config = cfg.value;
    if (cfg.ok && cfg.value.password_required) {
      state.authState = "locked";
      openAuth();
    } else {
      state.ops = false;
      state.authState = "unknown";
      updateLockButton();
    }
    await refreshChrome();
    render();
  }

  window.addEventListener("DOMContentLoaded", init);
})();
