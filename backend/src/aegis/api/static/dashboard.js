/* Aegis Quant Dashboard — Console Tático layout (Fase 16).
 * Vanilla JS, no build step. Chart.js (CDN) is the one external dependency.
 * Every number rendered here comes from a real backend endpoint
 * (aegis/api/routes.py) backed by persisted data or a live exchange call -
 * nothing here invents or simulates a value. Where a metric genuinely isn't
 * available, the UI shows "—", never a guess.
 */

// -- account/exchange metadata --------------------------------------------
// Fase 17: BingX splits into _demo/_live variants per account (same API
// key, different balance/mode - see aegis.execution.bingx_session). Both
// are always tracked/shown (Overview, Positions, Risk & Logs); the
// dedicated BINGX tab focuses on whichever mode is currently ACTIVE
// (cache.bingxSettings.mode), so "trocar a chave/modo mostra a conta que
// estiver ativa" without hiding the dormant mode's history elsewhere.
const ACCOUNTS = {
  paper: { label: "Paper", icon: "P", exchange: null, kind: "paper" },
  shadow: { label: "Shadow", icon: "S", exchange: "binance", kind: "shadow" },
  shadow_bingx_demo: { label: "Shadow BingX (Demo)", icon: "S", exchange: "bingx", kind: "shadow" },
  shadow_bingx_live: { label: "Shadow BingX (Real)", icon: "S", exchange: "bingx", kind: "shadow" },
  momentum: { label: "Momentum", icon: "M", exchange: "binance", kind: "momentum" },
  momentum_bingx_demo: { label: "Momentum BingX (Demo)", icon: "M", exchange: "bingx", kind: "momentum" },
  momentum_bingx_live: { label: "Momentum BingX (Real)", icon: "M", exchange: "bingx", kind: "momentum" },
};
const EXCHANGE_ACCOUNTS = {
  binance: ["paper", "shadow", "momentum"],
  bingx: ["paper", "shadow_bingx_demo", "shadow_bingx_live", "momentum_bingx_demo", "momentum_bingx_live"],
};
const ACCENT_COLORS = {
  paper: "#a78bfa", shadow: "#f0b90b", shadow_bingx_demo: "#2b6bff", shadow_bingx_live: "#ff4d6a",
  momentum: "#f0b90b", momentum_bingx_demo: "#2bffa3", momentum_bingx_live: "#ffc857",
};

// -- tiny helpers ------------------------------------------------------
const el = (id) => document.getElementById(id);
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
  return new Date(iso).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
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

// -- theme (dark Console Tático default, light "claro" variant) ------------
function initTheme() {
  let saved = "dark";
  try { saved = localStorage.getItem("aegis-theme") || "dark"; } catch (e) { /* private mode etc - default stands */ }
  document.documentElement.setAttribute("data-theme", saved);
  updateThemeBtn(saved);
}
function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  updateThemeBtn(next);
  try { localStorage.setItem("aegis-theme", next); } catch (e) { /* ignore - per-viewer convenience only */ }
}
function updateThemeBtn(theme) {
  const btn = el("theme-toggle");
  if (btn) btn.textContent = theme === "dark" ? "☀" : "☾"; // sun / moon
}

// -- state ------------------------------------------------------------
let activeExchange = "bingx";
let activeTab = "overview";
let chartRange = "7D";
let ordersFilter = "all"; // account_id or "all"
let expandedBacktestRun = null;
const charts = {};

let cache = {
  overview: { accounts: {} },
  positions: {},
  equityHistory: {},
  journal: { entries: [] },
  momentumScan: { candidates: [] },
  newsAssetStatus: { statuses: [] },
  newsRecent: { items: [] },
  eventsUpcoming: { events: [] },
  backtests: { runs: [] },
  walkForward: { runs: [] },
  health: { candles: [] },
  macro: { series: [] },
  derivatives: { symbols: [] },
  liquidations: { symbols: [] },
  regime: { symbols: [] },
  killSwitchEvents: {}, // account_id -> events[]
  tradesByAccount: {}, // account_id -> trades[]
  bingxSettings: { mode: "demo", credentials_configured: false, updated_at: null },
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
  let totalEquity = 0, totalPnl = 0, openPositions = 0, activeKillSwitches = 0;
  for (const id of Object.keys(ACCOUNTS)) {
    const a = accounts[id];
    if (!a || !a.initialized) continue;
    totalEquity += a.equity;
    totalPnl += a.daily_realized_pnl;
    openPositions += a.open_positions_count;
    if (a.kill_switch.is_triggered) activeKillSwitches++;
  }
  el("overview-kpis").innerHTML = `
    <div class="kpi-tile accent"><div class="lbl">Patrimônio total</div><div class="val">${fmtMoney(totalEquity)}</div></div>
    <div class="kpi-tile"><div class="lbl">PnL do dia</div><div class="val ${totalPnl >= 0 ? "up" : "down"}">${fmtMoney(totalPnl)}</div></div>
    <div class="kpi-tile"><div class="lbl">Posições abertas</div><div class="val">${openPositions}</div></div>
    <div class="kpi-tile"><div class="lbl">Kill switches ativos</div><div class="val ${activeKillSwitches ? "down" : "up"}">${activeKillSwitches}</div></div>
    <div class="kpi-tile"><div class="lbl">Contas inicializadas</div><div class="val">${Object.values(accounts).filter((a) => a && a.initialized).length}/${Object.keys(ACCOUNTS).length}</div></div>
    <div class="kpi-tile"><div class="lbl">Risco geral</div><div class="val ${activeKillSwitches ? "down" : "up"}">${activeKillSwitches ? "ATENÇÃO" : "NORMAL"}</div></div>
  `;
  renderComboChart("overview-chart", Object.keys(ACCOUNTS));
  renderAccountSummaryTable("overview-accounts-table");
  renderHealth();
}

function renderAccountSummaryTable(tbodyId) {
  const accounts = cache.overview.accounts || {};
  const tbody = el(tbodyId);
  if (!tbody) return;
  tbody.innerHTML = Object.keys(ACCOUNTS).map((id) => {
    const a = accounts[id];
    const meta = ACCOUNTS[id];
    if (!a || !a.initialized) return `<tr><td>${meta.label}</td><td colspan="6" class="empty">Ainda não inicializada</td></tr>`;
    const ddClass = a.drawdown_pct > 0.075 ? "crit" : a.drawdown_pct > 0.05 ? "warn" : "ok";
    const ks = a.kill_switch.is_triggered ? `<span class="pill crit">DISPARADO</span>` : `<span class="pill ok">NORMAL</span>`;
    return `<tr>
      <td style="font-weight:700">${meta.label}</td>
      <td class="num">${fmtMoney(a.equity)}</td>
      <td class="num ${a.daily_realized_pnl >= 0 ? "up" : "down"}">${fmtMoney(a.daily_realized_pnl)}</td>
      <td><span class="pill ${ddClass}">${fmtPct(a.drawdown_pct)}</span></td>
      <td class="num">${a.consecutive_losses}</td>
      <td class="num">${a.open_positions_count}</td>
      <td>${ks}</td>
    </tr>`;
  }).join("");
}

// -- exchange view (binance/bingx tabs) -----------------------------------
function activeBingxMode() {
  return (cache.bingxSettings && cache.bingxSettings.mode) || "demo";
}

function renderExchangeView(exchange) {
  const accountIds = EXCHANGE_ACCOUNTS[exchange];
  const accounts = cache.overview.accounts || {};
  const primaryAccountId = exchange === "binance" ? "shadow" : `shadow_bingx_${activeBingxMode()}`;
  const primary = accounts[primaryAccountId];

  renderExchangeBanner(exchange, primary);
  // Rich single-account combo chart (equity line + per-trade PnL bars) for
  // the exchange's primary real-money-track account - matches the
  // Console Tático reference more closely than a flattened 3-line
  // comparison would; the Overview tab still shows all accounts together.
  // For BingX this always follows whichever mode is currently ACTIVE - the
  // dormant mode's own history is still fully visible via Overview/
  // Positions/Risk & Logs (never hidden), just not the tab's headline chart.
  renderComboChart(`chart-${exchange}`, [primaryAccountId]);
  renderActivityList(`activity-${exchange}`, accountIds);
  renderStrategyGrid(`strategies-${exchange}`, accountIds);
  const realAccounts = exchange === "binance"
    ? ["shadow", "momentum"]
    : ["shadow_bingx_demo", "shadow_bingx_live", "momentum_bingx_demo", "momentum_bingx_live"];
  // showAccountColumn=true here even though this table only ever holds
  // ONE exchange's positions - it still mixes Shadow and Momentum (and,
  // for BingX, Demo and Real), and without this column there was no way to
  // tell which strategy/mode opened a given position.
  renderPositionsTable(`positions-table-${exchange}`, realAccounts, true);
  renderRiskMonitor(`risk-${exchange}`, primaryAccountId);
  renderLogFeed(`feed-${exchange}`, accountIds);
  renderTimeline(`history-${exchange}`, accountIds);
}

function renderExchangeBanner(exchange, account) {
  const root = el(`banner-${exchange}`);
  if (!root) return;
  const strip = root.querySelector(".kpi-strip-inline");
  if (exchange === "bingx") {
    const live = activeBingxMode() === "live";
    root.querySelector(".pill-env").textContent = live ? "REAL · DINHEIRO DE VERDADE" : "DEMO · VST";
    root.querySelector(".pill-env").style.color = live ? "var(--red)" : "";
    root.querySelector(".pill-env").style.borderColor = live ? "var(--red)" : "";
    const sub = el("bingx-banner-sub");
    if (sub) sub.textContent = live ? "Operando com fundos reais na BingX" : "Ambiente de simulação (VST)";
  } else {
    root.querySelector(".pill-env").textContent = "TESTNET";
  }
  if (!account || !account.initialized) {
    strip.innerHTML = `<div class="kpi-tile"><div class="lbl">Status</div><div class="val">Não inicializada</div></div>`;
    return;
  }
  const dd = account.drawdown_pct;
  const ddClass = dd > 0.075 ? "down" : dd > 0.05 ? "" : "up";
  const risky = account.kill_switch.is_triggered;
  strip.innerHTML = `
    <div class="kpi-tile accent"><div class="lbl">Patrimônio</div><div class="val">${fmtMoney(account.equity)}</div></div>
    <div class="kpi-tile"><div class="lbl">PnL do dia</div><div class="val ${account.daily_realized_pnl >= 0 ? "up" : "down"}">${fmtMoney(account.daily_realized_pnl)}</div></div>
    <div class="kpi-tile"><div class="lbl">Drawdown</div><div class="val ${ddClass}">${fmtPct(dd)}</div></div>
    <div class="kpi-tile"><div class="lbl">Posições abertas</div><div class="val">${account.open_positions_count}</div></div>
    <div class="kpi-tile"><div class="lbl">Perdas seguidas</div><div class="val">${account.consecutive_losses}</div></div>
    <div class="kpi-tile ${risky ? "" : ""}"><div class="lbl">Risco</div><div class="val ${risky ? "down" : "up"}">${risky ? "ATENÇÃO" : "NORMAL"}</div></div>
  `;
}

// -- combo chart: equity (line) + daily pnl (bars) + drawdown (dashed) -----
const RANGE_MS = { "1D": 864e5, "7D": 7 * 864e5, "30D": 30 * 864e5, "90D": 90 * 864e5, Todos: Infinity };

function filterByRange(points) {
  const seed = points[0];
  const real = points.slice(1);
  const isUnbounded = !Number.isFinite(RANGE_MS[chartRange]);
  const cutoff = Date.now() - RANGE_MS[chartRange];
  const filteredReal = isUnbounded ? real : real.filter((p) => new Date(p.closed_at).getTime() >= cutoff);
  const anchorMs = filteredReal.length ? new Date(filteredReal[0].closed_at).getTime() - 3600_000 : Date.now() - 864e5;
  return [{ ...seed, x: new Date(anchorMs) }, ...filteredReal.map((p) => ({ ...p, x: new Date(p.closed_at) }))];
}

function renderComboChart(canvasId, accountIds) {
  const canvas = el(canvasId);
  if (!canvas) return;
  const isMulti = accountIds.length > 1;
  const datasets = [];

  accountIds.forEach((id) => {
    const hist = cache.equityHistory[id];
    const points = hist && hist.points ? filterByRange(hist.points) : [];
    datasets.push({
      type: "line", label: `${ACCOUNTS[id].label} — patrimônio`,
      data: points.map((p) => ({ x: p.x, y: p.equity })),
      borderColor: ACCENT_COLORS[id], backgroundColor: ACCENT_COLORS[id] + "22",
      borderWidth: 2, pointRadius: 0, tension: 0.2, fill: !isMulti, yAxisID: "y",
    });
    if (!isMulti) {
      datasets.push({
        type: "bar", label: `${ACCOUNTS[id].label} — PnL do trade`,
        data: points.slice(1).map((p) => ({ x: p.x, y: p.net_pnl })),
        backgroundColor: points.slice(1).map((p) => (p.net_pnl >= 0 ? "#2bffa355" : "#ff4d6a55")),
        yAxisID: "y1", barThickness: 6,
      });
    }
  });

  if (charts[canvasId]) {
    charts[canvasId].data.datasets = datasets;
    charts[canvasId].update("none");
    return;
  }
  const rootStyles = getComputedStyle(document.documentElement);
  const gridColor = rootStyles.getPropertyValue("--border").trim() || "#163027";
  const tickColor = rootStyles.getPropertyValue("--text-faint").trim() || "#4a6b5c";
  charts[canvasId] = new Chart(canvas.getContext("2d"), {
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { type: "time", time: { unit: chartRange === "1D" ? "hour" : "day" }, grid: { color: gridColor }, ticks: { color: tickColor, maxTicksLimit: 8, font: { family: "JetBrains Mono", size: 10 } } },
        y: { position: "left", grid: { color: gridColor }, ticks: { color: tickColor, font: { family: "JetBrains Mono", size: 10 } } },
        y1: { position: "right", grid: { display: false }, ticks: { color: tickColor, font: { family: "JetBrains Mono", size: 10 } }, display: !isMulti },
      },
      plugins: { legend: { display: true, labels: { color: tickColor, boxWidth: 10, font: { size: 10, family: "JetBrains Mono" } } } },
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
    options: { responsive: true, maintainAspectRatio: false, animation: false, scales: { x: { display: false }, y: { display: false } }, plugins: { legend: { display: false }, tooltip: { enabled: false } } },
  });
}

function setChartRange(range) {
  chartRange = range;
  qsa(".chart-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.range === range));
  renderAll();
}

// -- radial risk gauge (SVG, no chart lib needed) --------------------------
function radialGaugeSvg(pct, label, sublabel) {
  const r = 54, c = 2 * Math.PI * r;
  const offset = c * (1 - Math.min(1, Math.max(0, pct)));
  const color = pct > 0.85 ? "var(--red)" : pct > 0.6 ? "var(--amber)" : "var(--accent)";
  return `<svg viewBox="0 0 140 140" width="140" height="140">
    <circle cx="70" cy="70" r="${r}" fill="none" stroke="var(--border)" stroke-width="11"/>
    <circle cx="70" cy="70" r="${r}" fill="none" stroke="${color}" stroke-width="11" stroke-dasharray="${c}" stroke-dashoffset="${offset}" stroke-linecap="round" transform="rotate(-90 70 70)"/>
    <text x="70" y="66" text-anchor="middle" fill="var(--text)" font-size="22" font-weight="700" font-family="JetBrains Mono">${label}</text>
    <text x="70" y="84" text-anchor="middle" fill="var(--text-faint)" font-size="9" font-family="JetBrains Mono">${sublabel}</text>
  </svg>`;
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
    let dotClass = "", statusText = "Aguardando sinal...", pct = null;
    if (account && account.initialized) {
      pct = account.equity ? account.daily_realized_pnl / account.equity : 0;
      if (openCount > 0) { dotClass = "active"; statusText = "Operação em andamento"; }
      else if (account.daily_realized_pnl !== 0) { dotClass = "done"; statusText = "Operação encerrada"; }
      else { statusText = "Sem operações"; }
    }
    const pctHtml = pct === null ? "" : `<div class="pct ${pct >= 0 ? "up" : "down"}">${pct >= 0 ? "+" : ""}${fmtPct(pct, 2)}</div>`;
    return `<div class="activity-row"><div class="dot ${dotClass}"></div>
      <div class="info"><div class="t">${meta.label}</div><div class="s">${statusText}</div></div>${pctHtml}</div>`;
  }).join("");
}

// -- strategy cards -------------------------------------------------------
// Wins/losses derived from the same equity-history replay already fetched
// for the performance chart (real closed trades, net_pnl per point) - no
// extra request needed.
function winLossCounts(accountId) {
  const hist = cache.equityHistory[accountId];
  const trades = hist && hist.points ? hist.points.slice(1) : [];
  const wins = trades.filter((p) => p.net_pnl >= 0).length;
  const losses = trades.length - wins;
  return { wins, losses };
}

function renderStrategyGrid(elId, accountIds) {
  const root = el(elId);
  if (!root) return;
  const accounts = cache.overview.accounts || {};
  root.innerHTML = accountIds.map((id) => {
    const meta = ACCOUNTS[id];
    const a = accounts[id];
    const equity = a && a.initialized ? fmtMoney(a.equity) : "—";
    const pnl = a && a.initialized ? a.daily_realized_pnl : null;
    const { wins, losses } = winLossCounts(id);
    return `<div class="strategy-card">
      <div class="head"><div class="ic">${meta.icon}</div><div class="name">${meta.label}</div><div class="tag" ${id.endsWith("_live") ? 'style="color:var(--red);border-color:var(--red)"' : ""}>${id.endsWith("_live") ? "REAL" : "TESTE"}</div></div>
      <canvas id="spark-${id}" height="30"></canvas>
      <div class="body"><div><div class="lbl">Patrimônio</div><div class="v">${equity}</div></div>
      <div style="text-align:right"><div class="lbl">PnL dia</div><div class="v ${pnl >= 0 ? "up" : "down"}">${pnl === null ? "—" : fmtMoney(pnl)}</div></div></div>
      <div class="body" style="margin-top:-4px">
        <div><div class="lbl">Vitórias</div><div class="v up" style="font-size:12px">${wins}</div></div>
        <div style="text-align:right"><div class="lbl">Perdas</div><div class="v down" style="font-size:12px">${losses}</div></div>
      </div>
    </div>`;
  }).join("");
  accountIds.forEach((id) => renderSparkline(`spark-${id}`, id, ACCENT_COLORS[id]));
}

// -- positions table ------------------------------------------------------
function renderPositionsTable(tbodyId, accountIds, showAccountColumn) {
  const tbody = el(tbodyId);
  if (!tbody) return;
  const positions = cache.positions || {};
  const rows = [];
  for (const id of accountIds) {
    for (const p of positions[id] || []) rows.push({ account: id, ...p });
  }
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="${showAccountColumn ? 11 : 10}" class="empty">Nenhuma posição aberta</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((p) => {
    const hasLiveData = p.mark_price !== undefined && p.mark_price !== null;
    const pnl = hasLiveData ? p.unrealized_pnl : null;
    const statusPill = p.mirror_ok === false
      ? `<span class="pill crit" title="Corretora reporta posição fechada">⚠ DIVERGENTE</span>`
      : `<span class="pill ok">ABERTA</span>`;
    const accountCell = showAccountColumn ? `<td>${ACCOUNTS[p.account].label}</td>` : "";
    return `<tr>${accountCell}
      <td class="num">${p.symbol}</td>
      <td><span class="pill ${p.side === "LONG" ? "long" : "short"}">${p.side}</span></td>
      <td class="num">${fmtNum(p.entry_price)}</td>
      <td class="num">${fmtNum(p.quantity)}</td>
      <td class="num">${p.stop_price ? fmtNum(p.stop_price) : "—"}</td>
      <td class="num">${p.take_profit_price ? fmtNum(p.take_profit_price) : (p.momentum_score !== undefined ? `${p.momentum_score.toFixed(1)}%` : "—")}</td>
      <td class="num">${hasLiveData ? fmtNum(p.mark_price) : "—"}</td>
      <td class="num ${pnl >= 0 ? "up" : pnl < 0 ? "down" : ""}">${pnl === null ? "—" : fmtMoney(pnl)}</td>
      <td class="num">${fmtDuration(p.entry_time)}</td>
      <td>${statusPill}</td>
    </tr>`;
  }).join("");
}

// -- risk monitor (interativo/explicativo) ---------------------------------
function renderRiskMonitor(elId, accountId) {
  const root = el(elId);
  if (!root) return;
  const account = (cache.overview.accounts || {})[accountId];
  const positions = (cache.positions || {})[accountId] || [];
  if (!account || !account.initialized) { root.innerHTML = `<div class="empty">Conta ainda não inicializada</div>`; return; }

  const exposurePct = Math.min(1, positions.length / 10);
  root.innerHTML = `
    <div style="display:flex;gap:16px;align-items:center;margin-bottom:8px">
      <div class="gauge-wrap">${radialGaugeSvg(exposurePct, positions.length, "/10 POSIÇÕES")}</div>
      <div style="flex:1">
        <div class="risk-row"><span class="rl">Drawdown do dia</span><span class="rv">${fmtPct(account.drawdown_pct)}</span></div>
        <div class="progress"><div style="width:${Math.min(100, account.drawdown_pct / 0.10 * 100)}%;background:${account.drawdown_pct > 0.075 ? "var(--red)" : account.drawdown_pct > 0.05 ? "var(--amber)" : "var(--accent)"}"></div></div>
        <div class="risk-row" style="margin-top:6px"><span class="rl">Perdas seguidas</span><span class="rv">${account.consecutive_losses}</span></div>
        <div class="risk-row"><span class="rl">PnL do dia</span><span class="rv ${account.daily_realized_pnl >= 0 ? "up" : "down"}">${fmtMoney(account.daily_realized_pnl)}</span></div>
      </div>
    </div>
    <div class="explain-box ${account.drawdown_pct > 0.075 ? "crit" : account.drawdown_pct > 0.05 ? "" : "ok"}">
      <b>Drawdown:</b> acima de 5% o sistema entra em CAUTELA (reduz risco por trade em 25%); acima de 7.5%, REDUZIDO (corta mais); acima de 10%, o Kill Switch trava novas entradas.
    </div>
    <div class="explain-box ${account.consecutive_losses >= 5 ? "crit" : account.consecutive_losses >= 3 ? "" : "ok"}">
      <b>Perdas seguidas:</b> em 3, modo cautela; em 5, reduz o tamanho das próximas entradas; em 8, trava novas entradas até reset manual.
    </div>
    <div class="risk-row" style="margin-top:4px">
      <span class="rl">Kill Switch</span>
      <span style="display:flex;align-items:center;gap:8px"><span class="toggle ${account.kill_switch.is_triggered ? "" : "on"}"></span>
      <b class="${account.kill_switch.is_triggered ? "down" : "up"}">${account.kill_switch.is_triggered ? "DISPARADO" : "NORMAL"}</b></span>
    </div>
    ${account.kill_switch.is_triggered ? `
      <div class="explain-box crit"><b>Motivo:</b> ${escapeHtml((account.kill_switch.reasons || []).join(", "))}</div>
      <button class="icon-btn" style="width:auto;padding:6px 12px;margin-top:6px" onclick="resetKillSwitch('${accountId}')">RESETAR (auditado)</button>
    ` : ""}
  `;
}

// -- log-style execution feed (Execução) -----------------------------------
function renderLogFeed(elId, accountIds) {
  const root = el(elId);
  if (!root) return;
  const entries = (cache.journal.entries || []).filter((e) => accountIds.includes(e.account)).slice(0, 20);
  if (!entries.length) { root.innerHTML = `<div class="empty">Nenhuma atividade recente</div>`; return; }
  root.innerHTML = entries.map((e) => {
    const cls = e.net_pnl >= 0 ? "close-win" : "close-loss";
    return `<div class="log-line ${cls}">[<span class="ts">${fmtClock(e.closed_at)}</span>] CLOSE ${e.symbol.padEnd(13)} ${e.side.padEnd(5)} ${e.exit_reason.padEnd(12)} pnl=${e.net_pnl >= 0 ? "+" : ""}${e.net_pnl.toFixed(2)} r=${e.r_multiple.toFixed(2)} <span style="color:var(--text-faint)">· ${ACCOUNTS[e.account] ? ACCOUNTS[e.account].label : e.account}</span></div>`;
  }).join("");
}

// -- timeline (Histórico) - grouped by strategy, not merged chronologically --
function renderTimeline(elId, accountIds) {
  const root = el(elId);
  if (!root) return;
  const entries = cache.journal.entries || [];
  const groups = accountIds.map((id) => ({
    id, label: ACCOUNTS[id].label, items: entries.filter((e) => e.account === id).slice(0, 5),
  }));
  if (!groups.some((g) => g.items.length)) { root.innerHTML = `<div class="empty">Nenhum evento ainda</div>`; return; }
  root.innerHTML = groups.map((g) => `
    <div style="margin-bottom:14px">
      <div style="font-size:10px;font-weight:700;color:var(--accent);font-family:'JetBrains Mono';letter-spacing:.5px;margin-bottom:8px;text-transform:uppercase">${g.label}</div>
      ${g.items.length ? renderTimelineItems(g.items) : `<div class="empty" style="padding:6px 0;text-align:left">Sem eventos ainda</div>`}
    </div>`).join("");
}
function renderTimelineItems(entries) {
  return entries.map((e) => {
    const isWin = e.net_pnl >= 0;
    return `<div class="timeline-item">
      <div class="tdot ${isWin ? "win" : "loss"}"></div>
      <div class="ttime">${fmtTime(e.closed_at)}</div>
      <div class="ttitle">${e.symbol} fechado (${e.exit_reason})</div>
      <div class="tdesc ${isWin ? "up" : "down"}">${e.side} · PnL ${fmtMoney(e.net_pnl)} · R ${e.r_multiple.toFixed(2)}</div>
      ${e.reasons && e.reasons.length ? `<div class="treason">&#8618; ${escapeHtml(e.reasons.join("; "))}</div>` : ""}
    </div>`;
  }).join("");
}

// -- strategies tab (all accounts) -----------------------------------------
function renderStrategiesTab() { renderStrategyGrid("strategies-all", Object.keys(ACCOUNTS)); }

// -- positions tab (consolidated) -------------------------------------------
function renderPositionsTab() { renderPositionsTable("positions-table-all", Object.keys(ACCOUNTS), true); }

// -- orders tab: per-strategy trade history + merged journal ----------------
function setOrdersFilter(accountId) {
  ordersFilter = accountId;
  qsa("#orders-chips .chip").forEach((c) => c.classList.toggle("active", c.dataset.account === accountId));
  renderOrdersTab();
}
async function ensureTradesLoaded(accountId) {
  if (cache.tradesByAccount[accountId]) return;
  const data = await getJSON(`/api/trades?account=${accountId}&limit=100`);
  cache.tradesByAccount[accountId] = data.trades;
}
function renderOrdersTab() {
  const tbody = el("orders-table");
  let rows;
  if (ordersFilter === "all") {
    rows = (cache.journal.entries || []).map((e) => ({ account: e.account, ...e }));
  } else {
    rows = (cache.tradesByAccount[ordersFilter] || []).map((t) => ({ account: ordersFilter, ...t }));
  }
  if (!rows.length) { tbody.innerHTML = `<tr><td colspan="7" class="empty">Nenhum trade ainda</td></tr>`; return; }
  tbody.innerHTML = rows.map((e) => `
    <tr>
      <td>${ACCOUNTS[e.account] ? ACCOUNTS[e.account].label : e.account}</td>
      <td class="num">${e.symbol}</td>
      <td><span class="pill ${e.side === "LONG" ? "long" : "short"}">${e.side}</span></td>
      <td class="num">${e.exit_reason}</td>
      <td class="num ${e.net_pnl >= 0 ? "up" : "down"}">${fmtMoney(e.net_pnl)}</td>
      <td class="num">${e.r_multiple.toFixed(2)}</td>
      <td class="num">${fmtTime(e.closed_at)}</td>
    </tr>`).join("");
}

// -- risk & logs tab --------------------------------------------------------
function renderRiskLogsTab() {
  const accounts = cache.overview.accounts || {};
  el("risk-logs-accounts").innerHTML = Object.keys(ACCOUNTS).map((id) => {
    const a = accounts[id];
    const meta = ACCOUNTS[id];
    if (!a || !a.initialized) return `<div class="card"><h2>${meta.label}</h2><div class="empty">Não inicializada</div></div>`;
    const ks = a.kill_switch.is_triggered
      ? `<span class="pill crit">DISPARADO</span> <button class="icon-btn" style="width:auto;padding:4px 9px;display:inline-flex;font-size:10px" onclick="resetKillSwitch('${id}')">RESET</button>`
      : `<span class="pill ok">NORMAL</span>`;
    const events = cache.killSwitchEvents[id] || [];
    return `<div class="card">
      <h2>${meta.label}</h2>
      <div class="risk-row"><span class="rl">Equity</span><span class="rv">${fmtMoney(a.equity)}</span></div>
      <div class="risk-row"><span class="rl">Drawdown</span><span class="rv">${fmtPct(a.drawdown_pct)}</span></div>
      <div class="risk-row"><span class="rl">Perdas seguidas</span><span class="rv">${a.consecutive_losses}</span></div>
      <div class="risk-row"><span class="rl">Kill Switch</span><span class="rv">${ks}</span></div>
      ${events.length ? `<div style="margin-top:8px;font-size:10px;color:var(--text-faint);font-family:'JetBrains Mono'">HISTÓRICO KILL SWITCH</div>
        ${events.slice(0, 4).map((ev) => `<div style="font-size:10px;color:var(--text-faint);font-family:'JetBrains Mono';padding:3px 0;border-top:1px solid var(--border-soft)">${fmtTime(ev.occurred_at)} · <span class="${ev.action === "TRIGGERED" ? "down" : "up"}">${ev.action}</span>${ev.note ? ` · ${escapeHtml(ev.note)}` : ""}</div>`).join("")}` : ""}
    </div>`;
  }).join("");

  renderHealth();
  renderNews();
  renderEvents();
  renderBacktests();
  renderWalkForward();
  renderMarketIntelligence();
}

function renderHealth() {
  const tbody = el("health-table");
  if (!tbody) return;
  const rows = cache.health.candles || [];
  tbody.innerHTML = rows.map((r) => `
    <tr><td class="num">${r.symbol}</td><td class="num">${r.interval}</td><td class="num">${fmtTime(r.latest_close_time)}</td>
    <td>${r.stale ? '<span class="pill crit">ATRASADO</span>' : '<span class="pill ok">OK</span>'}</td></tr>`).join("")
    || `<tr><td colspan="4" class="empty">Sem dados</td></tr>`;
}
function renderNews() {
  const statusBody = el("news-status-table");
  if (statusBody) {
    const pillClass = (status) => (status === "CLEAR" ? "ok" : status === "NEWS_CONFLICT" ? "crit" : "warn");
    statusBody.innerHTML = (cache.newsAssetStatus.statuses || []).map((s) => `
      <tr><td class="num">${s.asset}</td><td><span class="pill ${pillClass(s.status)}">${s.status}</span></td>
      <td class="num">${s.distinct_sources ?? "—"}</td><td class="num">${s.dominant_sentiment ?? "—"}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Sem dados</td></tr>`;
  }
  const recentBody = el("news-recent-table");
  if (recentBody) {
    const items = cache.newsRecent.items || [];
    recentBody.innerHTML = items.slice(0, 15).map((n) => `
      <tr><td class="num">${n.source_id}</td><td>${escapeHtml(n.title)}</td><td>${n.sentiment}</td></tr>`).join("")
      || `<tr><td colspan="3" class="empty">Nenhuma notícia coletada ainda</td></tr>`;
  }
}
function renderEvents() {
  const tbody = el("events-table");
  if (!tbody) return;
  tbody.innerHTML = (cache.eventsUpcoming.events || []).map((e) => `
    <tr><td class="num">${fmtTime(e.date_event)}</td><td>${escapeHtml(e.title)}</td><td class="num">${(e.coins || []).join(", ")}</td></tr>`).join("")
    || `<tr><td colspan="3" class="empty">Sem eventos</td></tr>`;
}

function toggleBacktestRun(runId) {
  expandedBacktestRun = expandedBacktestRun === runId ? null : runId;
  renderBacktests();
  if (expandedBacktestRun !== null) loadBacktestTrades(runId);
}
async function loadBacktestTrades(runId) {
  const container = el(`backtest-trades-${runId}`);
  if (!container) return;
  container.innerHTML = `<div class="empty">Carregando trades…</div>`;
  try {
    const data = await getJSON(`/api/backtests/${runId}/trades`);
    if (!data.trades.length) { container.innerHTML = `<div class="empty">Nenhum trade neste run</div>`; return; }
    container.innerHTML = `<table><thead><tr><th>Símbolo</th><th>Lado</th><th>Entrada</th><th>Saída</th><th>PnL</th><th>R</th></tr></thead><tbody>
      ${data.trades.map((t) => `<tr><td class="num">${t.symbol}</td><td><span class="pill ${t.side === "LONG" ? "long" : "short"}">${t.side}</span></td>
        <td class="num">${fmtTime(t.entry_time)}</td><td class="num">${fmtTime(t.exit_time)}</td>
        <td class="num ${t.net_pnl >= 0 ? "up" : "down"}">${fmtMoney(t.net_pnl)}</td><td class="num">${t.r_multiple.toFixed(2)}</td></tr>`).join("")}
      </tbody></table>`;
  } catch (e) {
    container.innerHTML = `<div class="empty">Erro ao carregar: ${escapeHtml(e.message)}</div>`;
  }
}
function renderBacktests() {
  const tbody = el("backtests-table");
  if (!tbody) return;
  const runs = cache.backtests.runs || [];
  if (!runs.length) { tbody.innerHTML = `<tr><td colspan="6" class="empty">Sem backtests</td></tr>`; return; }
  tbody.innerHTML = runs.map((r) => {
    const isOpen = expandedBacktestRun === r.id;
    return `<tr class="clickable" onclick="toggleBacktestRun(${r.id})">
      <td class="num">${isOpen ? "▾" : "▸"} ${r.symbol}</td><td class="num">${r.total_trades}</td>
      <td class="num">${fmtPct(r.win_rate)}</td>
      <td class="num ${r.net_pnl >= 0 ? "up" : "down"}">${fmtMoney(r.net_pnl)}</td>
      <td>${r.kill_switch_triggered ? '<span class="pill crit">DISPAROU</span>' : '<span class="pill ok">OK</span>'}</td>
      <td class="num">${fmtTime(r.created_at)}</td>
    </tr>${isOpen ? `<tr><td colspan="6" style="padding:0"><div id="backtest-trades-${r.id}" style="padding:10px"></div></td></tr>` : ""}`;
  }).join("");
}

function renderWalkForward() {
  const tbody = el("walk-forward-table");
  if (!tbody) return;
  const runs = cache.walkForward.runs || [];
  if (!runs.length) { tbody.innerHTML = `<tr><td colspan="7" class="empty">Sem walk-forward runs ainda — rode scripts/run_walk_forward.py</td></tr>`; return; }
  tbody.innerHTML = runs.map((r) => `
    <tr>
      <td class="num">${r.symbol}</td>
      <td class="num">${r.fold_count}</td>
      <td><span class="pill ${r.profitable_fold_pct >= 0.6 ? "ok" : r.profitable_fold_pct >= 0.4 ? "warn" : "crit"}">${fmtPct(r.profitable_fold_pct)}</span></td>
      <td class="num ${r.average_net_pnl >= 0 ? "up" : "down"}">${fmtMoney(r.average_net_pnl)}</td>
      <td class="num down">${fmtMoney(r.worst_fold_net_pnl)}</td>
      <td>${r.any_fold_kill_switch_triggered ? '<span class="pill crit">DISPAROU</span>' : '<span class="pill ok">OK</span>'}</td>
      <td class="num">${fmtTime(r.created_at)}</td>
    </tr>`).join("");
}

function renderMarketIntelligence() {
  const macroBody = el("macro-table");
  if (macroBody) {
    macroBody.innerHTML = (cache.macro.series || []).map((s) => `
      <tr><td>${s.name || s.series_id}<div style="font-size:9.5px;color:var(--text-faint)">${s.series_id}</div></td>
      <td class="num">${s.value !== null && s.value !== undefined ? fmtNum(s.value, 2) : "—"}</td>
      <td class="num ${s.change_pct >= 0 ? "up" : s.change_pct < 0 ? "down" : ""}">${s.change_pct !== null && s.change_pct !== undefined ? fmtPct(s.change_pct / 100, 2) : "—"}</td>
      <td class="num">${s.as_of ? fmtTime(s.as_of) : "—"}</td>
      <td><span class="pill ${s.quality === "OK" ? "ok" : "neutral"}">${s.quality || "SEM DADOS"}</span></td></tr>`).join("")
      || `<tr><td colspan="5" class="empty">Sem séries macro configuradas</td></tr>`;
  }
  const derivBody = el("derivatives-table");
  if (derivBody) {
    derivBody.innerHTML = (cache.derivatives.symbols || []).map((s) => `
      <tr><td class="num">${s.symbol}</td>
      <td class="num">${s.funding_rate !== null ? fmtPct(s.funding_rate, 4) : "—"}</td>
      <td class="num">${s.open_interest !== null ? fmtNum(s.open_interest, 0) : "—"}</td>
      <td class="num">${s.long_short_ratio !== null ? fmtNum(s.long_short_ratio, 3) : "—"}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Sem dados</td></tr>`;
  }
  const liqBody = el("liquidations-table");
  if (liqBody) {
    liqBody.innerHTML = (cache.liquidations.symbols || []).map((s) => `
      <tr><td class="num">${s.symbol}</td>
      <td class="num down">${fmtMoney(s.long_notional)}</td>
      <td class="num up">${fmtMoney(s.short_notional)}</td>
      <td class="num">${s.long_count + s.short_count}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Sem liquidações na última hora</td></tr>`;
  }
  const regimeBody = el("regime-table");
  if (regimeBody) {
    const trendPill = (t) => (
      t === "TRENDING_UP" ? '<span class="pill long">ALTA</span>'
      : t === "TRENDING_DOWN" ? '<span class="pill short">BAIXA</span>'
      : t === "RANGING" ? '<span class="pill neutral">LATERAL</span>'
      : '<span class="pill neutral">INDEFINIDO</span>'
    );
    const volPill = (v) => (
      v === "HIGH_VOLATILITY" ? '<span class="pill warn">ALTA</span>'
      : v === "LOW_VOLATILITY" ? '<span class="pill ok">BAIXA</span>'
      : v === "NORMAL_VOLATILITY" ? '<span class="pill neutral">NORMAL</span>'
      : "—"
    );
    regimeBody.innerHTML = (cache.regime.symbols || []).map((s) => `
      <tr><td class="num">${s.symbol}</td>
      <td>${trendPill(s.trend_regime)}</td>
      <td class="num">${s.adx_14 !== null ? fmtNum(s.adx_14, 1) : "—"}</td>
      <td>${volPill(s.volatility_regime)}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">Sem dados</td></tr>`;
  }
}

// -- BingX account settings (Fase 17 - demo/live switch) --------------------
const BINGX_LIVE_CONFIRM_PHRASE = "ATIVAR CONTA REAL";

function toggleBingxSettings() {
  const panel = el("bingx-settings-panel");
  if (!panel) return;
  panel.style.display = panel.style.display === "none" ? "" : "none";
  if (panel.style.display !== "none") renderBingxSettingsPanel();
}

function renderBingxSettingsPanel() {
  const s = cache.bingxSettings;
  const credsEl = el("bingx-creds-status");
  if (credsEl) {
    credsEl.innerHTML = s.credentials_configured
      ? `<span class="pill ok">CONFIGURADA</span>`
      : `<span class="pill warn">NÃO CONFIGURADA</span>`;
  }
  const modeEl = el("bingx-current-mode");
  if (modeEl) {
    modeEl.innerHTML = s.mode === "live"
      ? `<span class="pill crit">REAL — DINHEIRO DE VERDADE</span>`
      : `<span class="pill ok">DEMO (VST)</span>`;
  }
  const demoBtn = el("bingx-mode-demo-btn");
  const liveBtn = el("bingx-mode-live-btn");
  if (demoBtn) demoBtn.classList.toggle("active", s.mode === "demo");
  if (liveBtn) liveBtn.classList.toggle("active", s.mode === "live");
}

window.toggleBingxSettings = toggleBingxSettings;

window.saveBingxCredentials = async function () {
  const apiKey = el("bingx-api-key-input").value.trim();
  const apiSecret = el("bingx-api-secret-input").value.trim();
  const status = el("bingx-save-status");
  if (!apiKey || !apiSecret) { status.textContent = "Preencha os dois campos."; status.className = "settings-status down"; return; }
  status.textContent = "Validando na BingX...";
  status.className = "settings-status";
  try {
    const r = await fetch("/api/settings/bingx/credentials", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { status.textContent = body.detail || `Falha (HTTP ${r.status})`; status.className = "settings-status down"; return; }
    status.textContent = "Chave salva com sucesso.";
    status.className = "settings-status up";
    el("bingx-api-key-input").value = "";
    el("bingx-api-secret-input").value = "";
    cache.bingxSettings = await getJSON("/api/settings/bingx");
    renderBingxSettingsPanel();
  } catch (e) {
    status.textContent = `Erro: ${e.message}`;
    status.className = "settings-status down";
  }
};

window.switchBingxMode = async function (mode) {
  const status = el("bingx-mode-status");
  const current = cache.bingxSettings.mode;
  if (mode === current) return;
  let confirm_ = undefined;
  if (mode === "live") {
    confirm_ = prompt(
      `Isto ativa a conta REAL da BingX — o sistema passa a operar com dinheiro de verdade.\n` +
      `Digite exatamente "${BINGX_LIVE_CONFIRM_PHRASE}" para confirmar:`
    );
    if (confirm_ === null) return; // cancelled
  }
  status.textContent = "Trocando...";
  status.className = "settings-status";
  try {
    const r = await fetch("/api/settings/bingx/mode", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, confirm: confirm_ }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) { status.textContent = body.detail || `Falha (HTTP ${r.status})`; status.className = "settings-status down"; return; }
    status.textContent = `Modo alterado para ${mode === "live" ? "REAL" : "DEMO"}.`;
    status.className = "settings-status up";
    cache.bingxSettings = await getJSON("/api/settings/bingx");
    renderBingxSettingsPanel();
    await refreshData();
    renderAll();
  } catch (e) {
    status.textContent = `Erro: ${e.message}`;
    status.className = "settings-status down";
  }
};

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
  } catch (e) { alert(`Erro: ${e.message}`); }
};
window.toggleBacktestRun = toggleBacktestRun;
window.toggleTheme = toggleTheme;
window.setOrdersFilter = setOrdersFilter;

// -- master render dispatch ------------------------------------------------
function renderAll() {
  renderOverviewTab();
  renderExchangeView("binance");
  renderExchangeView("bingx");
  renderBingxSettingsPanel();
  renderStrategiesTab();
  renderPositionsTab();
  renderOrdersTab();
  renderRiskLogsTab();
}

// -- data refresh --------------------------------------------------------
async function refreshData() {
  const accountIds = Object.keys(ACCOUNTS);
  const [overview, positions, journal, momentumScan, newsAssetStatus, newsRecent, eventsUpcoming, backtests, walkForward, health, macro, derivatives, liquidations, regime, bingxSettings] = await Promise.all([
    getJSON("/api/overview"),
    getJSON("/api/positions"),
    getJSON("/api/journal?limit=50"),
    getJSON("/api/momentum/scan"),
    getJSON("/api/news/asset-status"),
    getJSON("/api/news/recent?limit=15"),
    getJSON("/api/events/upcoming?limit=20"),
    getJSON("/api/backtests?limit=15"),
    getJSON("/api/walk-forward?limit=15"),
    getJSON("/api/system/health"),
    getJSON("/api/market/macro"),
    getJSON("/api/market/derivatives"),
    getJSON("/api/market/liquidations"),
    getJSON("/api/market/regime"),
    getJSON("/api/settings/bingx").catch(() => cache.bingxSettings),
  ]);
  const equityHistoryPairs = await Promise.all(
    accountIds.map((id) => getJSON(`/api/equity-history?account=${id}&limit=500`).catch(() => ({ points: [{ closed_at: null, equity: 0 }] }))),
  );
  const equityHistory = {};
  accountIds.forEach((id, i) => { equityHistory[id] = equityHistoryPairs[i]; });

  const killSwitchEventPairs = await Promise.all(
    accountIds.map((id) => getJSON(`/api/kill-switch/${id}/events?limit=5`).catch(() => ({ events: [] }))),
  );
  const killSwitchEvents = {};
  accountIds.forEach((id, i) => { killSwitchEvents[id] = killSwitchEventPairs[i].events; });

  if (ordersFilter !== "all") await ensureTradesLoaded(ordersFilter);

  cache = {
    overview, positions, equityHistory, journal, momentumScan,
    newsAssetStatus, newsRecent, eventsUpcoming, backtests, walkForward, health,
    macro, derivatives, liquidations, regime, killSwitchEvents, bingxSettings,
    tradesByAccount: cache.tradesByAccount,
  };
}

async function refresh() {
  try {
    await refreshData();
    renderAll();
    el("last-update").textContent = `ÚLTIMA ATUALIZAÇÃO: ${new Date().toLocaleTimeString("pt-BR")}`;
  } catch (e) {
    el("last-update").textContent = `ERRO: ${e.message}`;
  }
}

// -- wiring ----------------------------------------------------------------
function initTabs() {
  qsa(".tab-btn").forEach((btn) => btn.addEventListener("click", () => setActiveTab(btn.dataset.tab)));
  qsa(".exchange-toggle button").forEach((btn) => btn.addEventListener("click", () => setActiveExchange(btn.dataset.exchange)));
  qsa(".chart-tabs button").forEach((btn) => btn.addEventListener("click", () => setChartRange(btn.dataset.range)));
  const themeBtn = el("theme-toggle");
  if (themeBtn) themeBtn.addEventListener("click", toggleTheme);
}

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initTabs();
  setActiveTab("bingx");
  refresh();
  setInterval(refresh, 10000);
});
