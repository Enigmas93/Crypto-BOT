/* Aegis Quant Dashboard (Fase 16 redesign) - vanilla JS, no build step,
 * matches the rest of this project's "plain HTML/CSS/JS served as a
 * static file" approach. Chart.js is the one external dependency, loaded
 * via CDN in index.html.
 *
 * Every number rendered here comes from a real backend endpoint
 * (aegis/api/routes.py) backed by real persisted data or a live exchange
 * call - nothing in this file invents or simulates a value. Where a
 * metric genuinely isn't available (e.g. Paper has no live exchange
 * position to check), the UI shows "—", never a guess.
 */

// -- account/exchange metadata --------------------------------------------
const ACCOUNTS = {
  paper: { label: "Paper", icon: "P", exchange: null, kind: "paper" },
  shadow: { label: "Shadow", icon: "S", exchange: "binance", kind: "shadow" },
  shadow_bingx: { label: "Shadow BingX", icon: "S", exchange: "bingx", kind: "shadow" },
  momentum: { label: "Momentum", icon: "M", exchange: "binance", kind: "momentum" },
  momentum_bingx: { label: "Momentum BingX", icon: "M", exchange: "bingx", kind: "momentum" },
};
const EXCHANGE_ACCOUNTS = {
  binance: ["paper", "shadow", "momentum"],
  bingx: ["paper", "shadow_bingx", "momentum_bingx"],
};
const ACCENT_COLORS = {
  paper: "#a78bfa", shadow: "#3b82f6", shadow_bingx: "#2b6bff",
  momentum: "#f59e0b", momentum_bingx: "#22c55e",
};

// -- tiny helpers ------------------------------------------------------
const el = (id) => document.getElementById(id);
const qs = (sel, root = document) => root.querySelector(sel);
const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function fmtMoney(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function fmtPct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}
function fmtNum(v, digits = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString("en-US", { maximumFractionDigits: digits });
}
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function fmtClock(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function fmtDuration(startIso) {
  if (!startIso) return "—";
  const ms = Date.now() - new Date(startIso).getTime();
  if (ms < 0) return "—";
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ${mins % 60}m`;
  return `${Math.floor(hrs / 24)}d ${hrs % 24}h`;
}
async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}`);
  return r.json();
}

// -- state ------------------------------------------------------------
let activeExchange = "bingx"; // matches the mockup's default focus
let activeTab = "overview";
let chartRange = "7D";
const charts = {}; // canvasId -> Chart.js instance

// -- data cache (refreshed on each poll) --------------------------------
let cache = {
  overview: { accounts: {} },
  positions: {},
  equityHistory: {}, // account -> {points:[...]}
  journal: { entries: [] },
  momentumScan: { candidates: [] },
  newsAssetStatus: { statuses: [] },
  newsRecent: { items: [] },
  eventsUpcoming: { events: [] },
  backtests: { runs: [] },
  health: { candles: [] },
};

// -- tab / exchange switching --------------------------------------------
function setActiveTab(tab) {
  activeTab = tab;
  qsa(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  qsa(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${tab}`));
  renderAll();
}
function setActiveExchange(ex) {
  activeExchange = ex;
  qsa(".exchange-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.exchange === ex));
  if (activeTab === "binance" || activeTab === "bingx") setActiveTab(ex);
  else renderAll();
}

// -- overview tab --------------------------------------------------------
function renderOverviewTab() {
  const accounts = cache.overview.accounts || {};
  let totalEquity = 0, totalPnl = 0, openPositions = 0, activeKillSwitches = 0, anyInit = false;
  for (const id of Object.keys(ACCOUNTS)) {
    const a = accounts[id];
    if (!a || !a.initialized) continue;
    anyInit = true;
    totalEquity += a.equity;
    totalPnl += a.daily_realized_pnl;
    openPositions += a.open_positions_count;
    if (a.kill_switch.is_triggered) activeKillSwitches++;
  }
  const kpiRow = el("overview-kpis");
  kpiRow.innerHTML = `
    <div class="kpi-card"><div class="lbl">Patrimônio total</div><div class="val">${fmtMoney(totalEquity)}</div>
      <div class="sub ${totalPnl >= 0 ? "up" : "down"}">${totalPnl >= 0 ? "▲" : "▼"} ${fmtMoney(totalPnl)} hoje</div></div>
    <div class="kpi-card"><div class="lbl">Posições abertas</div><div class="val">${openPositions}</div>
      <div class="sub" style="color:var(--text-faint)">em ${Object.keys(ACCOUNTS).length} contas</div></div>
    <div class="kpi-card"><div class="lbl">Kill Switches ativos</div><div class="val ${activeKillSwitches ? "down" : "up"}">${activeKillSwitches}</div>
      <div class="sub" style="color:var(--text-faint)">${activeKillSwitches ? "verifique Risk & Logs" : "tudo normal"}</div></div>
    <div class="kpi-card"><div class="lbl">Contas inicializadas</div><div class="val">${anyInit ? Object.values(accounts).filter(a => a.initialized).length : 0}/${Object.keys(ACCOUNTS).length}</div>
      <div class="sub" style="color:var(--text-faint)">Paper · Shadow · Shadow BingX · Momentum · Momentum BingX</div></div>
  `;

  renderMultiEquityChart("overview-chart", Object.keys(ACCOUNTS));
  renderAccountSummaryTable("overview-accounts-table");
  renderHealth();
}

function renderAccountSummaryTable(tbodyId) {
  const accounts = cache.overview.accounts || {};
  const tbody = el(tbodyId);
  if (!tbody) return;
  const rows = Object.keys(ACCOUNTS).map((id) => {
    const a = accounts[id];
    const meta = ACCOUNTS[id];
    if (!a || !a.initialized) {
      return `<tr><td>${meta.label}</td><td colspan="6" class="empty">Ainda não inicializada</td></tr>`;
    }
    const ddClass = a.drawdown_pct > 0.075 ? "crit" : a.drawdown_pct > 0.05 ? "warn" : "ok";
    const ks = a.kill_switch.is_triggered
      ? `<span class="pill crit">Disparado</span>` : `<span class="pill ok">Normal</span>`;
    return `<tr>
      <td><span class="pill open" style="background:transparent;color:var(--text);font-weight:700;">${meta.label}</span></td>
      <td class="mono">${fmtMoney(a.equity)}</td>
      <td class="mono ${a.daily_realized_pnl >= 0 ? "up" : "down"}">${fmtMoney(a.daily_realized_pnl)}</td>
      <td><span class="pill ${ddClass}">${fmtPct(a.drawdown_pct)}</span></td>
      <td class="mono">${a.consecutive_losses}</td>
      <td class="mono">${a.open_positions_count}</td>
      <td>${ks}</td>
    </tr>`;
  }).join("");
  tbody.innerHTML = rows;
}

// -- exchange view (binance/bingx tabs) -----------------------------------
function renderExchangeView(exchange) {
  const accountIds = EXCHANGE_ACCOUNTS[exchange];
  const accounts = cache.overview.accounts || {};
  const primaryAccountId = exchange === "binance" ? "shadow" : "shadow_bingx";
  const primary = accounts[primaryAccountId];

  renderExchangeBanner(exchange, primary);
  renderMultiEquityChart(`chart-${exchange}`, accountIds);
  renderActivityList(`activity-${exchange}`, accountIds);
  renderStrategyGrid(`strategies-${exchange}`, accountIds);
  renderPositionsTable(`positions-table-${exchange}`, exchange === "binance" ? ["shadow", "momentum"] : ["shadow_bingx", "momentum_bingx"]);
  renderRiskMonitor(`risk-${exchange}`, primaryAccountId);
  renderExecutionFeed(`feed-${exchange}`, accountIds);
}

function renderExchangeBanner(exchange, account) {
  const root = el(`banner-${exchange}`);
  if (!root) return;
  const label = exchange === "binance" ? "Binance" : "BingX";
  const envLabel = exchange === "binance" ? "TESTNET" : "VST · TESTNET";
  if (!account || !account.initialized) {
    root.querySelector(".stat-strip").innerHTML = `<div class="stat"><div class="lbl">Status</div><div class="val">Conta ainda não inicializada</div></div>`;
    return;
  }
  const dd = account.drawdown_pct;
  const ddClass = dd > 0.075 ? "down" : dd > 0.05 ? "" : "up";
  root.querySelector(".stat-strip").innerHTML = `
    <div class="stat"><div class="lbl">Patrimônio</div><div class="val">${fmtMoney(account.equity)}</div></div>
    <div class="stat"><div class="lbl">Pico</div><div class="val">${fmtMoney(account.peak_equity)}</div></div>
    <div class="stat"><div class="lbl">PnL do dia</div><div class="val ${account.daily_realized_pnl >= 0 ? "up" : "down"}">${fmtMoney(account.daily_realized_pnl)}</div></div>
    <div class="stat"><div class="lbl">Drawdown</div><div class="val ${ddClass}">${fmtPct(dd)}</div></div>
    <div class="stat"><div class="lbl">Posições abertas</div><div class="val">${account.open_positions_count}</div></div>
    <div class="stat"><div class="lbl">Perdas seguidas</div><div class="val">${account.consecutive_losses}</div></div>
    <div class="stat"><div class="lbl">Risco</div><div class="val ${account.kill_switch.is_triggered ? "down" : "up"}">${account.kill_switch.is_triggered ? "DISPARADO" : "NORMAL"}</div></div>
  `;
  qs(`#env-badge-${exchange}`).textContent = envLabel;
}

// -- equity chart (Chart.js) ----------------------------------------------
const RANGE_MS = { "1D": 864e5, "7D": 7 * 864e5, "30D": 30 * 864e5, "90D": 90 * 864e5, Todos: Infinity };

function filterByRange(points) {
  // points[0] is always the "seed" (starting equity, closed_at=null) -
  // real trade points follow in chronological order.
  const seed = points[0];
  const real = points.slice(1);
  const isUnbounded = !Number.isFinite(RANGE_MS[chartRange]);
  const cutoff = Date.now() - RANGE_MS[chartRange];
  const filteredReal = isUnbounded ? real : real.filter((p) => new Date(p.closed_at).getTime() >= cutoff);
  // The seed point needs a real x-value to plot (its own closed_at is
  // null, by definition, since it's before any trade) - anchored just
  // before the first real point shown, or one day back if there is no
  // trade history at all yet, never Infinity/NaN.
  const anchorMs = filteredReal.length
    ? new Date(filteredReal[0].closed_at).getTime() - 3600_000
    : Date.now() - 864e5;
  return [{ ...seed, x: new Date(anchorMs) }, ...filteredReal.map((p) => ({ ...p, x: new Date(p.closed_at) }))];
}

function renderMultiEquityChart(canvasId, accountIds) {
  const canvas = el(canvasId);
  if (!canvas) return;
  const datasets = accountIds.map((id) => {
    const hist = cache.equityHistory[id];
    const points = hist && hist.points ? filterByRange(hist.points) : [];
    return {
      label: ACCOUNTS[id].label,
      data: points.map((p) => ({ x: p.x, y: p.equity })),
      borderColor: ACCENT_COLORS[id],
      backgroundColor: ACCENT_COLORS[id] + "22",
      borderWidth: 2, pointRadius: 0, tension: 0.25, fill: accountIds.length === 1,
    };
  });
  if (charts[canvasId]) {
    charts[canvasId].data.datasets = datasets;
    charts[canvasId].update("none");
    return;
  }
  charts[canvasId] = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { type: "time", time: { unit: chartRange === "1D" ? "hour" : "day" }, grid: { color: "#171d33" }, ticks: { color: "#6b7796", maxTicksLimit: 8 } },
        y: { grid: { color: "#171d33" }, ticks: { color: "#6b7796" } },
      },
      plugins: { legend: { display: accountIds.length > 1, labels: { color: "#a3adc7", boxWidth: 10, font: { size: 10.5 } } } },
    },
  });
}

function renderSparkline(canvasId, accountId, color) {
  const canvas = el(canvasId);
  if (!canvas) return;
  const hist = cache.equityHistory[accountId];
  const points = hist && hist.points ? hist.points.slice(-20) : [];
  const data = points.map((p) => p.equity);
  if (charts[canvasId]) {
    charts[canvasId].data.datasets[0].data = data;
    charts[canvasId].update("none");
    return;
  }
  charts[canvasId] = new Chart(canvas.getContext("2d"), {
    type: "line",
    data: { labels: data.map((_, i) => i), datasets: [{ data, borderColor: color, borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: false }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: { x: { display: false }, y: { display: false } },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
    },
  });
}

function setChartRange(range) {
  chartRange = range;
  qsa(".chart-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.range === range));
  renderAll();
}

// -- activity list ------------------------------------------------------
function renderActivityList(elId, accountIds) {
  const root = el(elId);
  if (!root) return;
  const accounts = cache.overview.accounts || {};
  const positions = cache.positions || {};
  root.innerHTML = accountIds.map((id) => {
    const meta = ACCOUNTS[id];
    const account = accounts[id];
    const openCount = (positions[id] || []).length;
    let dotClass = "";
    let statusText = "Aguardando sinal...";
    let pct = null;
    if (account && account.initialized) {
      pct = account.equity ? account.daily_realized_pnl / account.equity : 0;
      if (openCount > 0) { dotClass = "active"; statusText = "Operação em andamento"; }
      else if (account.daily_realized_pnl !== 0) { dotClass = "done"; statusText = "Operação encerrada"; }
      else { statusText = "Sem operações"; }
    }
    const pctHtml = pct === null ? "" : `<div class="pct ${pct >= 0 ? "up" : "down"}">${pct >= 0 ? "+" : ""}${fmtPct(pct, 2)}</div>`;
    return `<div class="activity-row">
      <div class="dot ${dotClass}"></div>
      <div class="info"><div class="t">${meta.label}</div><div class="s">${statusText}</div></div>
      ${pctHtml}
    </div>`;
  }).join("");
}

// -- strategy cards -------------------------------------------------------
function renderStrategyGrid(elId, accountIds) {
  const root = el(elId);
  if (!root) return;
  const accounts = cache.overview.accounts || {};
  root.innerHTML = accountIds.map((id) => {
    const meta = ACCOUNTS[id];
    const a = accounts[id];
    const equity = a && a.initialized ? fmtMoney(a.equity) : "—";
    const pnl = a && a.initialized ? a.daily_realized_pnl : null;
    return `<div class="strategy-card">
      <div class="head">
        <div class="ic">${meta.icon}</div>
        <div class="name">${meta.label}</div>
        <div class="tag">Teste</div>
      </div>
      <canvas id="spark-${id}" height="34"></canvas>
      <div class="body">
        <div><div class="lbl">Patrimônio</div><div class="v">${equity}</div></div>
        <div style="text-align:right"><div class="lbl">PnL do dia</div><div class="v ${pnl >= 0 ? "up" : "down"}">${pnl === null ? "—" : fmtMoney(pnl)}</div></div>
      </div>
    </div>`;
  }).join("");
  accountIds.forEach((id) => renderSparkline(`spark-${id}`, id, ACCENT_COLORS[id]));
}

// -- positions table ------------------------------------------------------
function renderPositionsTable(tbodyId, accountIds, showAccountColumn = false) {
  const tbody = el(tbodyId);
  if (!tbody) return;
  const positions = cache.positions || {};
  const rows = [];
  for (const id of accountIds) {
    for (const p of positions[id] || []) {
      rows.push({ account: id, ...p });
    }
  }
  if (!rows.length) {
    const colspan = showAccountColumn ? 11 : 10;
    tbody.innerHTML = `<tr><td colspan="${colspan}" class="empty">Nenhuma posição aberta</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((p) => {
    const hasLiveData = p.mark_price !== undefined && p.mark_price !== null;
    const pnl = hasLiveData ? p.unrealized_pnl : null;
    const mismatchWarning = p.mirror_ok === false ? `<span class="pill crit" title="Corretora reporta posição fechada - aguardando reconciliação">⚠ divergente</span>` : `<span class="pill ok">Aberta</span>`;
    const accountCell = showAccountColumn ? `<td>${ACCOUNTS[p.account].label}</td>` : "";
    return `<tr>
      ${accountCell}
      <td class="mono">${p.symbol}</td>
      <td><span class="pill ${p.side === "LONG" ? "long" : "short"}">${p.side}</span></td>
      <td class="mono">${fmtNum(p.entry_price)}</td>
      <td class="mono">${fmtNum(p.quantity)}</td>
      <td class="mono">${p.stop_price ? fmtNum(p.stop_price) : "—"}</td>
      <td class="mono">${p.take_profit_price ? fmtNum(p.take_profit_price) : (p.momentum_score !== undefined ? `${p.momentum_score.toFixed(1)}%` : "—")}</td>
      <td class="mono">${hasLiveData ? fmtNum(p.mark_price) : "—"}</td>
      <td class="mono ${pnl >= 0 ? "up" : pnl < 0 ? "down" : ""}">${pnl === null ? "—" : fmtMoney(pnl)}</td>
      <td class="mono">${fmtDuration(p.entry_time)}</td>
      <td>${mismatchWarning}</td>
    </tr>`;
  }).join("");
}

// -- risk monitor ---------------------------------------------------------
function renderRiskMonitor(elId, accountId) {
  const root = el(elId);
  if (!root) return;
  const account = (cache.overview.accounts || {})[accountId];
  const positions = (cache.positions || {})[accountId] || [];
  if (!account || !account.initialized) {
    root.innerHTML = `<div class="empty">Conta ainda não inicializada</div>`;
    return;
  }
  const exposurePct = Math.min(1, positions.length / 10); // rough visual only - real max_open_positions is 10
  root.innerHTML = `
    <div class="risk-row"><span class="rl">Posições abertas</span><span class="rv">${positions.length}<span class="rmax">/ 10 máx.</span></span></div>
    <div class="progress"><div style="width:${exposurePct * 100}%"></div></div>
    <div class="risk-row"><span class="rl">Drawdown</span><span class="rv">${fmtPct(account.drawdown_pct)}</span></div>
    <div class="risk-row"><span class="rl">Perdas seguidas</span><span class="rv">${account.consecutive_losses}</span></div>
    <div class="risk-row"><span class="rl">PnL do dia</span><span class="rv ${account.daily_realized_pnl >= 0 ? "up" : "down"}">${fmtMoney(account.daily_realized_pnl)}</span></div>
    <div class="risk-row"><span class="rl">Kill Switch</span><span class="rv"><span class="toggle ${account.kill_switch.is_triggered ? "" : "on"}"></span></span></div>
    ${account.kill_switch.is_triggered ? `<div class="risk-row"><span class="rl">Motivo</span><span class="rv down" style="font-size:11px">${escapeHtml((account.kill_switch.reasons || []).join(", "))}</span></div>
    <div style="margin-top:10px"><button class="icon-btn" style="width:auto;padding:6px 12px" onclick="resetKillSwitch('${accountId}')">Resetar Kill Switch</button></div>` : ""}
  `;
}

// -- execution feed (derived from real closed trades - see journal endpoint) --
function renderExecutionFeed(elId, accountIds) {
  const root = el(elId);
  if (!root) return;
  const entries = (cache.journal.entries || []).filter((e) => accountIds.includes(e.account)).slice(0, 15);
  if (!entries.length) {
    root.innerHTML = `<div class="empty">Nenhuma atividade recente</div>`;
    return;
  }
  root.innerHTML = entries.map((e) => {
    const isWin = e.net_pnl >= 0;
    return `<div class="feed-row">
      <div class="fic ${isWin ? "buy" : "sell"}">${isWin ? "▲" : "▼"}</div>
      <div class="ftext">
        <div class="ftitle">Posição fechada — ${e.symbol} (${e.exit_reason})</div>
        <div class="fdesc">${ACCOUNTS[e.account] ? ACCOUNTS[e.account].label : e.account} · ${e.side} · PnL ${fmtMoney(e.net_pnl)} · R ${e.r_multiple.toFixed(2)}</div>
      </div>
      <div class="ftime">${fmtClock(e.closed_at)}</div>
    </div>`;
  }).join("");
}

// -- strategies tab (all accounts, fuller detail) -------------------------
function renderStrategiesTab() {
  renderStrategyGrid("strategies-all", Object.keys(ACCOUNTS));
}

// -- positions tab (consolidated) ------------------------------------------
function renderPositionsTab() {
  renderPositionsTable("positions-table-all", Object.keys(ACCOUNTS), true);
}

// -- orders tab (closed trades - no granular per-order log is persisted) --
function renderOrdersTab() {
  const tbody = el("orders-table");
  const entries = cache.journal.entries || [];
  if (!entries.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">Nenhum trade ainda</td></tr>`;
    return;
  }
  tbody.innerHTML = entries.map((e) => `
    <tr>
      <td>${ACCOUNTS[e.account] ? ACCOUNTS[e.account].label : e.account}</td>
      <td class="mono">${e.symbol}</td>
      <td><span class="pill ${e.side === "LONG" ? "long" : "short"}">${e.side}</span></td>
      <td class="mono">${e.exit_reason}</td>
      <td class="mono ${e.net_pnl >= 0 ? "up" : "down"}">${fmtMoney(e.net_pnl)}</td>
      <td class="mono">${e.r_multiple.toFixed(2)}</td>
      <td class="mono">${fmtTime(e.closed_at)}</td>
    </tr>`).join("");
}

// -- risk & logs tab --------------------------------------------------------
function renderRiskLogsTab() {
  const accounts = cache.overview.accounts || {};
  const root = el("risk-logs-accounts");
  root.innerHTML = Object.keys(ACCOUNTS).map((id) => {
    const a = accounts[id];
    const meta = ACCOUNTS[id];
    if (!a || !a.initialized) return `<div class="card"><h2>${meta.label}</h2><div class="empty">Não inicializada</div></div>`;
    const ks = a.kill_switch.is_triggered
      ? `<span class="pill crit">DISPARADO</span> <button class="icon-btn" style="width:auto;padding:5px 10px;display:inline-flex" onclick="resetKillSwitch('${id}')">Reset</button>`
      : `<span class="pill ok">Normal</span>`;
    return `<div class="card">
      <h2>${meta.label}</h2>
      <div class="risk-row"><span class="rl">Equity</span><span class="rv">${fmtMoney(a.equity)}</span></div>
      <div class="risk-row"><span class="rl">Drawdown</span><span class="rv">${fmtPct(a.drawdown_pct)}</span></div>
      <div class="risk-row"><span class="rl">Perdas seguidas</span><span class="rv">${a.consecutive_losses}</span></div>
      <div class="risk-row"><span class="rl">Kill Switch</span><span class="rv">${ks}</span></div>
      ${a.kill_switch.reasons && a.kill_switch.reasons.length ? `<div class="risk-row"><span class="rl">Motivo</span><span class="rv" style="font-size:11px;color:var(--text-faint)">${escapeHtml(a.kill_switch.reasons.join(", "))}</span></div>` : ""}
    </div>`;
  }).join("");

  renderHealth();
  renderNews();
  renderEvents();
  renderBacktests();
}

function renderHealth() {
  const tbody = el("health-table");
  if (!tbody) return;
  const rows = cache.health.candles || [];
  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td class="mono">${r.symbol}</td>
      <td class="mono">${r.interval}</td>
      <td class="mono">${fmtTime(r.latest_close_time)}</td>
      <td>${r.stale ? '<span class="pill crit">Atrasado</span>' : '<span class="pill ok">OK</span>'}</td>
    </tr>`).join("") || `<tr><td colspan="4" class="empty">Sem dados</td></tr>`;
}
function renderNews() {
  const statusBody = el("news-status-table");
  if (statusBody) {
    statusBody.innerHTML = (cache.newsAssetStatus.statuses || []).map((s) => `
      <tr><td class="mono">${s.asset}</td><td><span class="pill ${s.status === "CLEAR" ? "ok" : "warn"}">${s.status}</span></td>
      <td class="mono">${s.source_count ?? "—"}</td><td class="mono">${s.sentiment ?? "—"}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Sem dados</td></tr>`;
  }
  const recentBody = el("news-recent-table");
  if (recentBody) {
    recentBody.innerHTML = (cache.newsRecent.items || []).slice(0, 12).map((n) => `
      <tr><td class="mono">${n.source_id}</td><td>${escapeHtml(n.title)}</td><td>${n.sentiment}</td></tr>`).join("")
      || `<tr><td colspan="3" class="empty">Sem dados</td></tr>`;
  }
}
function renderEvents() {
  const tbody = el("events-table");
  if (!tbody) return;
  tbody.innerHTML = (cache.eventsUpcoming.events || []).map((e) => `
    <tr><td class="mono">${fmtTime(e.date_event)}</td><td>${escapeHtml(e.title)}</td><td class="mono">${(e.coins || []).join(", ")}</td></tr>`).join("")
    || `<tr><td colspan="3" class="empty">Sem eventos</td></tr>`;
}
function renderBacktests() {
  const tbody = el("backtests-table");
  if (!tbody) return;
  tbody.innerHTML = (cache.backtests.runs || []).map((r) => `
    <tr><td class="mono">${r.symbol}</td><td class="mono">${r.total_trades}</td>
    <td class="mono">${fmtPct(r.win_rate)}</td>
    <td class="mono ${r.net_pnl >= 0 ? "up" : "down"}">${fmtMoney(r.net_pnl)}</td>
    <td>${r.kill_switch_triggered ? '<span class="pill crit">Disparado</span>' : '<span class="pill ok">OK</span>'}</td></tr>`).join("")
    || `<tr><td colspan="5" class="empty">Sem backtests</td></tr>`;
}

// -- kill switch reset -----------------------------------------------------
window.resetKillSwitch = async function (accountId) {
  const note = prompt(`Motivo do reset do Kill Switch para "${accountId}" (obrigatório):`);
  if (!note || !note.trim()) return;
  try {
    const r = await fetch(`/api/kill-switch/${accountId}/reset`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note }),
    });
    if (!r.ok) { alert(`Falha ao resetar: HTTP ${r.status}`); return; }
    await refreshData();
    renderAll();
  } catch (e) {
    alert(`Erro: ${e.message}`);
  }
};

// -- master render dispatch ------------------------------------------------
function renderAll() {
  renderOverviewTab();
  renderExchangeView("binance");
  renderExchangeView("bingx");
  renderStrategiesTab();
  renderPositionsTab();
  renderOrdersTab();
  renderRiskLogsTab();
}

// -- data refresh --------------------------------------------------------
async function refreshData() {
  const accountIds = Object.keys(ACCOUNTS);
  const [overview, positions, journal, momentumScan, newsAssetStatus, newsRecent, eventsUpcoming, backtests, health] = await Promise.all([
    getJSON("/api/overview"),
    getJSON("/api/positions"),
    getJSON("/api/journal?limit=50"),
    getJSON("/api/momentum/scan"),
    getJSON("/api/news/asset-status"),
    getJSON("/api/news/recent?limit=15"),
    getJSON("/api/events/upcoming?limit=20"),
    getJSON("/api/backtests?limit=15"),
    getJSON("/api/system/health"),
  ]);
  const equityHistoryPairs = await Promise.all(
    accountIds.map((id) => getJSON(`/api/equity-history?account=${id}&limit=500`).catch(() => ({ points: [{ closed_at: null, equity: 0 }] }))),
  );
  const equityHistory = {};
  accountIds.forEach((id, i) => { equityHistory[id] = equityHistoryPairs[i]; });

  cache = {
    overview, positions, equityHistory, journal, momentumScan,
    newsAssetStatus, newsRecent, eventsUpcoming, backtests, health,
  };
}

async function refresh() {
  try {
    await refreshData();
    renderAll();
    el("last-update").textContent = `Última atualização: ${new Date().toLocaleTimeString("pt-BR")}`;
  } catch (e) {
    el("last-update").textContent = `Erro ao atualizar: ${e.message}`;
  }
}

// -- wiring ----------------------------------------------------------------
function initTabs() {
  qsa(".tab-btn").forEach((btn) => btn.addEventListener("click", () => setActiveTab(btn.dataset.tab)));
  qsa(".exchange-toggle button").forEach((btn) => btn.addEventListener("click", () => setActiveExchange(btn.dataset.exchange)));
  qsa(".chart-tabs button").forEach((btn) => btn.addEventListener("click", () => setChartRange(btn.dataset.range)));
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  setActiveTab("bingx");
  refresh();
  setInterval(refresh, 10000);
});
