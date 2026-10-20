/* Trader V2 — Professional dark paper-trading command center. Same-origin API. */
const TABS = ["Overview", "Market", "Signals", "Positions", "Backtest", "Risk", "Settings"];
const ICONS = { Overview: "📊", Market: "📈", Signals: "🎯", Positions: "💼", Backtest: "⏱", Risk: "🛡", Settings: "⚙" };
let cur = localStorage.getItem("tab") || "Overview";
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
let sessionOk = sessionStorage.getItem("authed") === "1";
let savedPwd = sessionStorage.getItem("authedPwd") || "";
let refreshTimer = null;

const toast = (m, kind = "") => {
  const d = document.createElement("div");
  d.className = "toast" + (kind ? " " + kind : "");
  d.textContent = String(m).slice(0, 400);
  $("#toasts").appendChild(d);
  setTimeout(() => d.remove(), 8000);
};

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (x, dec = 2) => { const n = Number(x); if (!Number.isFinite(n)) return "—"; return (n >= 0 ? "+" : "") + n.toFixed(dec); };
const pct = (x) => { const n = Number(x); if (!Number.isFinite(n)) return "—%"; return (n >= 0 ? "+" : "") + n.toFixed(2) + "%"; };

async function api(path, opts = {}) {
  const base = window.API_BASE_URL || window.FINRL_API_BASE_URL || "";
  const r = await fetch(base + path, opts);
  const t = await r.text();
  let j; try { j = JSON.parse(t); } catch { j = { detail: t }; }
  if (!r.ok) {
    const d = j.detail !== undefined ? j.detail : t.slice(0, 300);
    const m = (j.code ? `[${j.code}] ` : `[${r.status}] `) + (typeof d === "string" ? d : JSON.stringify(d).slice(0, 400));
    toast(m);
    throw new Error(m);
  }
  return j;
}

async function apiSafe(path, opts = {}) {
  try { return await api(path, opts); } catch (e) { return { _error: String(e.message) }; }
}

function candles(canvas, bars) {
  const c = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth * 2, H = canvas.height = 300;
  c.fillStyle = "#0b0c11"; c.fillRect(0, 0, W, H);
  if (!bars || !bars.length) { c.fillStyle = "#8b949e"; c.font = "24px sans-serif"; c.fillText("No data", 22, 40); return; }
  const hi = Math.max(...bars.map(b => b.h)), lo = Math.min(...bars.map(b => b.l));
  const Y = (p) => H - 26 - ((p - lo) / ((hi - lo) || 1)) * (H - 60);
  const cw = W / bars.length;
  bars.forEach((b, i) => {
    const up = b.c >= b.o;
    c.strokeStyle = c.fillStyle = up ? "#3fb950" : "#f85149";
    c.beginPath(); c.moveTo(i * cw + cw / 2, Y(b.h)); c.lineTo(i * cw + cw / 2, Y(b.l)); c.stroke();
    const y1 = Y(Math.max(b.o, b.c)), y2 = Y(Math.min(b.o, b.c));
    c.fillRect(i * cw + 1, y1, cw - 2, Math.max(2, y2 - y1));
  });
}

function lineChart(canvas, xs) {
  const c = canvas.getContext("2d");
  const W = canvas.width = canvas.clientWidth * 2, H = canvas.height = 240;
  c.fillStyle = "#0b0c11"; c.fillRect(0, 0, W, H);
  if (!xs || !xs.length) { c.fillStyle = "#8b949e"; c.font = "22px sans-serif"; c.fillText("No data", 20, 30); return; }
  const hi = Math.max(...xs), lo = Math.min(...xs);
  c.strokeStyle = "#2f81f7"; c.lineWidth = 3; c.beginPath();
  xs.forEach((e, i) => {
    const x = (i / Math.max(1, xs.length - 1)) * (W - 20) + 10;
    const y = H - 24 - ((e - lo) / ((hi - lo) || 1)) * (H - 44);
    i ? c.lineTo(x, y) : c.moveTo(x, y);
  });
  c.stroke();
}

function pill(cls, txt) { return `<span class="pill2 ${cls}">${esc(txt)}</span>`; }

const PAGES = {};

PAGES.Overview = async (el) => {
  const [s, cfg, summ, auto] = await Promise.allSettled([
    apiSafe("/api/health"), apiSafe("/api/config/validate"), apiSafe("/api/v2/summary"), apiSafe("/api/v2/automate/status")
  ]);
  const h = s.status === "fulfilled" ? s.value : {};
  const c = cfg.status === "fulfilled" ? cfg.value : {};
  const sm = summ.status === "fulfilled" ? summ.value : {};
  const aa = auto.status === "fulfilled" ? auto.value : {};
  const up = (h.components && h.components.risk_kill) ? (h.components.risk_kill.ok ? "OK" : "ENGAGED") : "—";
  const live = !!c.live;
  el.innerHTML = `
  <div class="grid4">
    <div class="stat"><div class="s-label">Mode</div><div class="s-val">${esc(c.trading_mode || "paper")}</div><div class="s-cap">${live ? '<span class="pill2 bad">LIVE</span>' : '<span class="pill2 ok">PAPER</span>'}</div></div>
    <div class="stat"><div class="s-label">Account Equity</div><div class="s-val">$${sm.equity !== undefined ? money(sm.equity) : "—"}</div><div class="s-cap">${sm.status ? `broker: ${esc(sm.status)}` : "paper account"}</div></div>
    <div class="stat"><div class="s-label">Daily P&L</div><div class="s-val ${sm.day_pnl >= 0 ? 'ok' : 'bad'}">${money(sm.day_pnl)}</div><div class="s-cap">session P&L</div></div>
    <div class="stat"><div class="s-label">Unrealized P&L</div><div class="s-val ${sm.unrealized_pl >= 0 ? 'ok' : 'bad'}">${money(sm.unrealized_pl)}</div><div class="s-cap">open positions</div></div>
    <div class="stat"><div class="s-label">Drawdown</div><div class="s-val bad">${money(sm.drawdown_pct)}%</div><div class="s-cap">from peak $${money(sm.peak)}</div></div>
    <div class="stat"><div class="s-label">Buying Power</div><div class="s-val">$${sm.buying_power !== undefined ? money(sm.buying_power) : "—"}</div><div class="s-cap">available capital</div></div>
    <div class="stat"><div class="s-label">Kill Switch</div><div class="s-val ${up === "ENGAGED" ? 'bad' : 'ok'}">${up}</div><div class="s-cap">risk engine</div></div>
    <div class="stat"><div class="s-label">Positions</div><div class="s-val">${sm.positions !== undefined ? sm.positions : "—"}</div><div class="s-cap">open</div></div>
  </div>
  <div class="grid3">
    <div class="card"><h3>Automation</h3>
      <div class="kv"><span class="k">Status</span><b class="${aa.running ? 'ok' : 'mut'}">${aa.running ? "RUNNING" : "OFF"}</b></div>
      <div class="kv"><span class="k">Cycle</span><b>${aa.minutes ? aa.minutes + " min" : "—"}</b></div>
      <div class="kv"><span class="k">Mode</span><b>${aa.dry_run ? "DRY-RUN" : "PAPER LIVE"}</b></div>
      <div class="kv"><span class="k">Runs</span><b>${aa.runs ?? "0"}</b></div>
      <div class="kv"><span class="k">Orders (live/dry)</span><b>${aa.live_orders ?? 0} / ${aa.dry_orders ?? 0}</b></div>
      <div class="actions" style="margin-top:8px;">
        <button class="btn" id="autostart">${aa.running ? "Stop" : "Start"}</button>
        <button class="btn secondary" id="autorun">Run Once</button>
      </div>
      <div class="muted" id="automsg" style="margin-top:6px;">${esc(aa.last_result || "")}</div>
    </div>
    <div class="card"><h3>Configuration</h3>
      ${Object.entries({
        "Trading mode": c.trading_mode,
        "Symbols": c.n_symbols,
        "Timeframe": c.timeframe,
        "Paper orders": c.paper_orders ? "ON" : "OFF",
        "Live trading": c.live ? "ON (blocked)" : "OFF",
        "Take-profit": (c.take_profit_pct !== undefined ? c.take_profit_pct : sm.take_profit_pct !== undefined ? sm.take_profit_pct : 0) + "%",
        "Min confidence": aa.automate_min_conf ?? c.automate?.min_conf ?? "—"
      }).map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}
      <p class="mut" style="margin-top:8px;">Secrets in .env, never shown. Live trading disabled by design.</p>
    </div>
    <div class="card"><h3>System Health</h3>
      ${h.components ? Object.entries(h.components).map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><b class="${v.ok ? 'ok' : 'bad'}">${v.ok ? "ok" : "FAIL"}</b> <span class="mut">${esc(v.detail || "")}</span></div>`).join("") : `<div class="empty">Health unavailable</div>`}
      <div class="mut" style="margin-top:6px;">${h.ts ? "updated " + esc(h.ts) : ""}</div>
    </div>
  </div>`;
  const autostartBtn = el.querySelector("#autostart");
  if (autostartBtn) {
    autostartBtn.onclick = async () => {
      try {
        const r = await api("/api/automate/" + (aa.running ? "stop" : "start"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: savedPwd }) });
        aa.running = !aa.running;
        autostartBtn.textContent = aa.running ? "Stop" : "Start";
        el.querySelector("#automsg").textContent = (r.result || "").slice(0, 200) || "updated";
      } catch (e) { toast(e.message); }
    };
  }
  const autorunBtn = el.querySelector("#autorun");
  if (autorunBtn) {
    autorunBtn.onclick = async () => {
      try {
        const r = await api("/api/automate/run-once", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: savedPwd }) });
        el.querySelector("#automsg").textContent = (r.result || "").slice(0, 200) || "ran";
      } catch (e) { toast(e.message); }
    };
  }
};

PAGES.Market = async (el) => {
  const cfg = await apiSafe("/api/config/validate");
  const syms = (cfg.status === "fulfilled" && cfg.value && cfg.value.symbols) ? cfg.value.symbols : ["BTC/USD", "ETH/USD", "SOL/USD"];
  el.innerHTML = `
  <div class="toolbar"><label>Symbol</label> <select id="mSym">${syms.map(s => `<option>${esc(s)}</option>`).join("")}</select>
    <button class="btn" id="mGo">Load</button> <span class="mut" id="mInfo">—</span></div>
  <div class="chart-wrap"><canvas class="chart" id="mCh"></canvas></div>
  <div class="mut" id="mNote" style="margin-top:6px;">Candlestick data from configured provider; quality flags shown above.</div>`;
  const go = async () => {
    const s = el.querySelector("#mSym").value;
    el.querySelector("#mInfo").textContent = "loading…";
    try {
      const tf = (cfg.status === "fulfilled" && cfg.value) ? cfg.value.timeframe : "15Min";
      const d = await api(`/api/market/bars?symbol=${encodeURIComponent(s)}&timeframe=${encodeURIComponent(tf)}&limit=120`);
      el.querySelector("#mInfo").textContent = d.quality.ok ? `${d.quality.rows} rows` : "QUALITY: " + d.quality.issues.join("; ");
      candles(el.querySelector("#mCh"), d.bars);
      if (!d.quality.ok) toast("Stale or incomplete market data for " + esc(s), "warn");
    } catch (e) { el.querySelector("#mInfo").textContent = "Error: " + e.message; toast(e.message); }
  };
  el.querySelector("#mGo").onclick = go;
  go();
};

PAGES.Signals = async (el) => {
  const s = await apiSafe("/api/signals"), muse = await apiSafe("/api/v2/muse");
  const sigs = s.status === "fulfilled" ? s.value : {};
  const keys = Object.keys(sigs || {});
  const mu = muse.status === "fulfilled" ? muse.value : {};
  el.innerHTML = `
  <div class="card"><h3>Muse AI Decision Status</h3>
    <div class="kv"><span class="k">Configured</span><b>${mu.enabled ? "yes" : "no"}</b></div>
    <div class="kv"><span class="k">Available</span><b class="${mu.available ? 'ok' : 'bad'}">${mu.available ? "yes" : "no"}</b></div>
    <div class="kv"><span class="k">Model</span><b>${esc(mu.model || "—")}</b></div>
    <div class="kv"><span class="k">Mode</span><b>fail-closed</b></div>
    <div class="kv"><span class="k">Failures</span><b>${mu.failures !== undefined ? mu.failures : "—"}</b> <span class="k">Last error</span><b class="bad">${esc(mu.last_error || "—")}</b></div>
    <div class="mut" style="margin-top:6px;">Muse is decision-support only, not a trading authority. Unavailable decisions are shown as ABSTAIN; no orders are placed automatically.</div>
  </div>
  <div class="card"><h3>Strategy Signals (${keys.length})</h3>
    ${keys.length ? keys.map(k => {
      const v = sigs[k] || {};
      const a = v.aggregate || { action: "ABSTAIN", score: 0, conf: 0, reason: "no data" };
      const cls = a.action === "BUY" ? "ok" : (a.action === "SELL" ? "bad" : "mut");
      return `<div class="card sig-card" style="margin:0;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <div class="sig-sym" style="font-weight:700;">${esc(k)}</div><div class="sig-act ${cls}">${a.action}</div>
        </div>
        <div class="kv"><span class="k">Score</span><b>${a.score}</b><span class="k">Conf</span><b>${a.conf}</b><span class="k">Disagree</span><b>${a.disagree ?? "—"}</b></div>
        <div class="muted sig-reason">${esc(a.reason || "")}</div>
        <table style="margin-top:6px;"><tr><th>Strategy</th><th>Sig</th><th>Conf</th><th>Reason</th></tr>
        ${(v.signals || []).map((x) => `<tr><td>${esc(x.name || "?")}</td><td>${x.signal ?? "?"}</td><td>${x.confidence ?? "?"}</td><td class="mut">${esc(x.reason || "")}</td></tr>`).join("")}</table>
      </div>`;
    }).join("") : `<div class="empty">No signals yet. Run the pipeline from Overview.</div>`}
  </div>`;
};

PAGES.Positions = async (el) => {
  const pos = await apiSafe("/api/positions", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const orders = await apiSafe("/api/v2/orders");
  const summ = await apiSafe("/api/v2/summary");
  const p = pos.status === "fulfilled" ? pos.value : {};
  const plist = p.positions || [];
  const eq = p.account ? p.account.equity : 0;
  const tu = p.total_unrealized_pl ?? 0;
  const tup = p.total_unrealized_pl_pct ?? 0;
  const tp = (summ.status === "fulfilled" && summ.value) ? summ.value.take_profit_pct : 0;
  const o = orders.status === "fulfilled" ? orders.value : {};
  const openOrders = o.open || [];
  const hist = o.history || [];
  el.innerHTML = `
  <div class="card"><h3>Paper Account</h3>
    <div class="kv"><span class="k">Equity</span><b>$${eq.toFixed(2)}</b><span class="k">Buying Power</span><b>$${p.account ? p.account.bp.toFixed(2) : "—"}</b></div>
    <div class="kv"><span class="k">Status</span><b>${p.account ? p.account.status : "—"}</b><span class="k">${tu >= 0 ? "Unrealized P&L" : "Unrealized P&L"}</span><b class="${tu >= 0 ? 'ok' : 'bad'}">$${tu.toFixed(2)} (${tup.toFixed(2)}%)</b></div>
    <div class="mut" style="margin-top:6px;">Take-profit threshold: ${tp}% (configurable). Per-position exit status shown below.</div>
  </div>
  <div class="card"><h3>Open Positions (${plist.length})</h3>
    ${plist.length ? `<table><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Notional</th><th>Cost</th><th>P&L</th><th>%</th><th>Exit vs TP</th></tr>
    ${plist.map((x) => {
      const up = Number(x.unrealized_pl || 0);
      const cost = Number(x.cost_basis || 0);
      const tpReach = cost > 0 && tp > 0 ? ((up / cost) * 100).toFixed(2) + "% of " + tp + "%" : "—";
      return `<tr><td>${esc(x.symbol)}</td><td>${esc(x.side)}</td><td>${Number(x.qty).toFixed(4)}</td><td>$${Number(x.current_price).toFixed(2)}</td><td>$${Number(x.market_value).toFixed(2)}</td><td>$${cost.toFixed(2)}</td><td class="${up >= 0 ? 'ok' : 'bad'}">$${up.toFixed(2)}</td><td class="${up >= 0 ? 'ok' : 'bad'}">${Number(x.unrealized_pl_pct).toFixed(2)}%</td><td class="mut">${tpReach}</td></tr>`;
    }).join("")}</table>` : `<p class="mut">Flat — no open positions.</p>`}</div>
  <div class="card"><h3>Pending / Open Orders (${openOrders.length})</h3>
    ${openOrders.length ? `<table><tr><th>Symbol</th><th>Side</th><th>Notional</th><th>Qty</th><th>Status</th><th>Client Order ID</th></tr>
    ${openOrders.map((x) => `<tr><td>${esc(x.symbol)}</td><td>${esc(x.side)}</td><td>$${Number(x.notional || 0).toFixed(2)}</td><td>${Number(x.qty || 0).toFixed(4)}</td><td class="mut">${esc(x.status)}</td><td class="mut">${esc(x.client_order_id || "—")}</td></tr>`).join("")}</table>
    <p class="mut">${o.open_error ? "Broker open-orders note: " + esc(o.open_error) : "Queried live from broker."}</p>` : `<p class="mut">No open orders.</p>`}</div>
  <div class="card"><h3>Execution History (fills, last 50)</h3>
    ${hist.length ? `<table><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Client Order ID</th><th>Note</th></tr>
    ${hist.slice().reverse().map((x) => `<tr><td class="mut">${esc(String(x.ts || "—")).slice(0,16)}</td><td>${esc(x.symbol || "—")}</td><td>${esc(x.side || "—")}</td><td>${Number(x.qty || 0).toFixed(4)}</td><td class="${x.status === "filled" ? 'ok' : (x.status === "rejected" ? 'bad' : 'mut')}">${esc(x.status || "—")}</td><td class="mut">${esc(x.cid || x.client_order_id || "—")}</td><td class="mut">${esc(x.note || "")}</td></tr>`).join("")}</table>` : `<p class="mut">No executions recorded this session.</p>`}</div>`;
};

PAGES.Backtest = async (el) => {
  el.innerHTML = `
  <div class="toolbar"><select id="bSym"><option>BTC/USD</option><option>ETH/USD</option><option>SOL/USD</option></select>
    <button class="btn" id="bGo">Run</button> <span class="mut" id="bInfo">—</span></div>
  <div class="chart-wrap"><canvas class="chart" id="bCh"></canvas></div>
  <div class="card"><h3>Metrics</h3><pre id="bMet">—</pre></div>
  <div class="mut" id="bAssumptions" style="margin-top:6px;"></div>`;
  const go = async () => {
    try {
      const j = await api("/api/backtest", { method: "POST", body: JSON.stringify({ symbol: el.querySelector("#bSym").value }) });
      el.querySelector("#bInfo").textContent = "job " + j.job_id + " — running…";
      for (let i = 0; i < 40; i++) {
        await new Promise(r => setTimeout(r, 1500));
        const st = await api("/api/jobs/" + j.job_id);
        if (st.status === "done") {
          lineChart(el.querySelector("#bCh"), st.result.curve || []);
          el.querySelector("#bMet").textContent = JSON.stringify(st.result.metrics, null, 1);
          el.querySelector("#bAssumptions").textContent = "Assumptions: fee " + (st.result.assumptions && st.result.assumptions.fee_pct !== undefined ? st.result.assumptions.fee_pct : "—") + ", slippage " + (st.result.assumptions && st.result.assumptions.slippage_pct !== undefined ? st.result.assumptions.slippage_pct : "—");
          el.querySelector("#bInfo").textContent = "done";
          return;
        }
        if (st.status === "failed") { el.querySelector("#bInfo").textContent = "failed: " + st.error; return; }
      }
      el.querySelector("#bInfo").textContent = "still running; refresh job manually";
    } catch (e) { toast(e.message); }
  };
  el.querySelector("#bGo").onclick = go;
};

PAGES.Risk = async (el) => {
  const r = await apiSafe("/api/risk");
  const v = r.status === "fulfilled" ? r.value : {};
  el.innerHTML = `
  <div class="card"><h3>Risk Controls</h3>
    ${Object.entries(v.limits || {}).map(([k, val]) => `<div class="kv"><span class="k">${esc(k)}</span><b>${typeof val === "number" ? val.toFixed(2) : esc(String(val))}</b></div>`).join("")}
    <div class="kv"><span class="k">Kill Switch</span><b class="${v.killed ? 'bad' : 'ok'}">${v.killed ? "ENGAGED" : "ARMED"}</b></div>
  </div>
  <div class="card"><h3>Emergency Stop</h3>
    <p class="mut">Blocks all new orders immediately. Existing positions require manual review. Paper trading only.</p>
    <label>Password <input type="password" id="killPwd" size="10"></label>
    <button class="btn stop" id="kBtn">Engage Kill Switch</button>
    <div class="muted" id="kMsg"></div></div>`;
  el.querySelector("#kBtn").onclick = async () => {
    if (!confirm("Engage kill switch? This blocks all new orders immediately.")) return;
    try {
      await api("/api/kill", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: $("#killPwd").value, reason: "dashboard manual" }) });
      el.querySelector("#kMsg").textContent = "Kill switch ENGAGED.";
      el.querySelector("#kBtn").disabled = true;
    } catch (e) { toast(e.message); }
  };
};

PAGES.Errors = async (el) => {
  const render = async () => {
    let server = [];
    try {
      const l = await api("/api/logs?limit=200");
      server = l.events.filter(e => e.level === "warning" || e.level === "error").reverse();
    } catch (e) { server = [{ ts: "-", level: "error", event: "log_fetch_failed", detail: e.message }]; }
    el.innerHTML = `<div class="card"><h3>Errors <button class="btn secondary" id="ref">Refresh</button> <button class="btn secondary" id="clr">Clear browser list</button></h3>
      <h4>Browser (this session: failed requests)</h4>
      ${clientErrors.length ? `<table><tr><th>Time</th><th>Request</th><th>Error</th></tr>
      ${clientErrors.map(e => `<tr><td class="mut">${e.ts}</td><td>${esc(e.where)}</td><td class="bad">${esc(e.msg)}</td></tr>`).join("")}</table>` : `<p class="mut">No browser errors this session.</p>`}
      <h4>Server (warnings + errors)</h4>
      ${server.length ? `<table><tr><th>Time</th><th>Level</th><th>Event</th><th>Detail</th></tr>
      ${server.map(e => `<tr><td class="mut">${e.ts || ""}</td><td class="bad">${e.level || ""}</td><td>${esc(e.event || "")}</td><td class="mut">${JSON.stringify(e.reason || e.status || e.path || e.detail || "").slice(0, 200)}</td></tr>`).join("")}</table>` : `<p class="ok">No server errors recorded.</p>`}</div>`;
    el.querySelector("#ref").onclick = render;
    el.querySelector("#clr").onclick = () => { clientErrors.length = 0; render(); };
  };
  await render();
};

PAGES.Settings = async (el) => {
  const c = await apiSafe("/api/config/validate");
  const cv = c.status === "fulfilled" ? c.value : {};
  el.innerHTML = `<div class="card"><h3>Configuration</h3>
    <pre>${esc(JSON.stringify(cv, null, 2))}</pre>
    <p class="mut">Secrets live in .env, never shown. Live trading unavailable by design.</p></div>`;
};

let clientErrors = [];

async function go() {
  const nav = $("#sidebarNav"); nav.innerHTML = "";
  TABS.forEach((t) => {
    const b = document.createElement("button");
    b.className = "btn " + (cur === t ? "on" : "");
    b.innerHTML = `<span class="ic">${ICONS[t] || "•"}</span> ${t}`;
    b.onclick = () => { cur = t; localStorage.setItem("tab", t); go(); };
    nav.appendChild(b);
  });
  const el = $("#page"); el.innerHTML = "<p class='mut'>Loading…</p>";
  try { await PAGES[cur](el); } catch (e) { el.innerHTML = `<div class="card bad">Error: ${esc(e.message)}</div>`; }
  try {
    const h = await api("/api/health");
    const c = $("#conn"); c.textContent = h.ok ? "LIVE" : "DEGRADED";
    c.className = "dot " + (h.ok ? "live" : "");
    if (h.components && h.components.config) {
      $("#sysMode").textContent = h.components.config.detail ? "configured" : "paper";
    }
  } catch { const c = $("#conn"); c.textContent = "OFFLINE"; c.className = "dot"; }
  $("#pageHeader").innerHTML = `<h1>${cur}</h1><div class="sub">${cur === "Overview" ? "System overview &amp; automation" : cur === "Market" ? "Market data &amp; charts" : cur === "Signals" ? "Strategy signals &amp; Muse AI status" : cur === "Positions" ? "Paper positions &amp; P&L" : cur === "Backtest" ? "Historical backtesting" : cur === "Risk" ? "Risk controls &amp; emergency stop" : "System configuration"}</div>`;
}

async function init() {
  if (!sessionOk) {
    $("#authModal").style.display = "flex";
    $("#authBtn").onclick = async () => {
      const pwd = $("#authPwd").value;
      try {
        await api("/api/auth", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pwd }) });
        sessionOk = true;
        sessionStorage.setItem("authed", "1");
        savedPwd = pwd;
        sessionStorage.setItem("authedPwd", pwd);
        $("#authModal").style.display = "none";
        go();
        startRefresh();
      } catch (e) {
        $("#authErr").textContent = "Invalid password. Try again.";
        $("#authPwd").value = "";
      }
    };
    $("#authPwd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#authBtn").click(); });
    return;
  }
  go();
  startRefresh();
  $("#btnRefresh").onclick = () => { go(); toast("Refreshed"); };
  $("#btnLogout").onclick = () => { sessionOk = false; sessionStorage.removeItem("authed"); sessionStorage.removeItem("authedPwd"); savedPwd = ""; location.reload(); };
  setInterval(async () => {
    try {
      const h = await api("/api/health");
      const c = $("#conn"); c.textContent = h.ok ? "LIVE" : "DEGRADED";
      c.className = "dot " + (h.ok ? "live" : "");
    } catch {}
  }, 15000);
}

function startRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => { if (document.visibilityState !== "hidden") go(); }, 60000);
}

window.addEventListener("DOMContentLoaded", init);
