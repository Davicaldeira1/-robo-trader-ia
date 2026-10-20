/* Painel do robô BTC/USDT (modo simulado). Sem frameworks: só JavaScript. */
"use strict";

const $ = (id) => document.getElementById(id);
const usd = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "USD" });
const num = (x, d = 2) => (x === null || x === undefined ? "—" :
  Number(x).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d }));
const money = (x) => (x === null || x === undefined ? "—" : usd.format(x));
const signed = (x) => (x === null || x === undefined ? "—" : (x > 0 ? "+" : x < 0 ? "−" : "") + usd.format(Math.abs(x)));
const pct = (x, d = 2) => (x === null || x === undefined ? "—" : (x > 0 ? "+" : x < 0 ? "−" : "") + num(Math.abs(x), d) + "%");
const when = (ms) => (ms ? new Date(ms).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—");
const TZ_SHIFT = -new Date().getTimezoneOffset() * 60; // gráficos mostram o horário do celular
const local = (sec) => sec + TZ_SHIFT;
const tone = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 3500);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    ...opts,
    headers: { "X-Requested-With": "painel", ...(opts.headers || {}) },
    credentials: "same-origin",
  });
  if (resp.status === 401) { location.href = "/login"; throw new Error("login"); }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `Erro ${resp.status}`);
  return body;
}

/* ------------------------------------------------------------ status */
let lastStatus = null;

function renderStatus(st) {
  lastStatus = st;
  const pill = $("status-pill");
  let cls = "ok", label = "Ativo";
  if (!st.motor_rodando) { cls = "err"; label = "Motor parado"; }
  else if (st.consecutive_failures >= 3) { cls = "err"; label = "Sem dados"; }
  else if (st.halted) { cls = "halted"; label = "Parado hoje (−3%)"; }
  else if (st.paused) { cls = "paused"; label = "Pausado"; }
  pill.className = "pill " + cls;
  pill.textContent = label;

  const warns = $("warnings");
  warns.replaceChildren();
  const warn = (text) => warns.append(el("div", { class: "warning", text }));
  if (!st.senha_configurada) warn("⚠️ Painel sem senha. Defina DASHBOARD_PASSWORD na hospedagem.");
  if (!st.telegram_configurado) warn("ℹ️ Telegram ainda não configurado (veja o README).");
  if (st.last_error) {
    const short = st.last_error.startsWith("Todas as fontes")
      ? "Sem dados de mercado: todas as fontes falharam. O robô não opera até normalizar (detalhes no registro abaixo)."
      : st.last_error.slice(0, 200);
    warn("❗ " + short);
  }

  // sinal
  const sig = st.signal;
  const act = $("signal-action");
  act.textContent = sig ? sig.action : "—";
  act.className = "signal " + (sig ? { COMPRA: "buy", VENDA: "sell" }[sig.action] || "wait" : "wait");
  $("signal-summary").textContent = sig ? sig.summary : "Aguardando o primeiro ciclo de análise…";
  $("price").textContent = st.price ? money(st.price) : "—";
  $("updated").textContent = st.last_success_ms ? "Atualizado " + when(st.last_success_ms) + (st.provider ? " · " + st.provider : "") : "sem dados ainda";
  const checks = $("checks");
  checks.replaceChildren();
  if (sig && sig.data_ok) {
    checks.append(el("li", { class: "grp", text: "Para COMPRAR (todas):" }));
    for (const c of sig.buy_checks) checks.append(checkItem(c));
    checks.append(el("li", { class: "grp", text: "Para VENDER (qualquer uma):" }));
    for (const c of sig.sell_checks) checks.append(checkItem(c));
  } else if (sig) {
    for (const r of sig.reasons) checks.append(el("li", {}, el("span", { text: "⚠️" }), el("span", { text: r })));
  }

  // blocos de saldo
  $("equity").textContent = money(st.equity);
  const d = $("equity-delta");
  d.textContent = `${signed(st.total_pnl)} (${pct(st.total_pnl_pct)}) desde o início de ${money(st.initial_balance)}`;
  d.className = "tile-sub " + tone(st.total_pnl);
  const dp = $("day-pnl");
  dp.textContent = signed(st.day_pnl);
  dp.className = "tile-value " + tone(st.day_pnl);
  const used = Math.min(100, st.day_loss_used_pct || 0);
  const meter = $("day-meter");
  meter.style.width = used + "%";
  meter.className = used >= 66 ? "hot" : "";
  $("day-limit").textContent = `${num(used, 0)}% do limite diário de ${money(st.daily_loss_cap)}`;
  const s = st.stats;
  $("win-rate").textContent = s.win_rate === null ? "—" : num(s.win_rate, 1) + "%";
  $("win-detail").textContent = `${s.wins} ganhos · ${s.losses} perdas`;
  $("drawdown").textContent = num(st.max_drawdown_pct, 2) + "%";
  $("drawdown-abs").textContent = "maior queda: " + money(st.max_drawdown_abs);
  const r = $("realized");
  r.textContent = signed(st.realized_pnl);
  r.className = "tile-value " + tone(st.realized_pnl);
  $("fees").textContent = `${s.trades} operações · taxas ${money(st.fees_paid)}`;

  // posição
  const pc = $("position-card");
  if (st.position) {
    const p = st.position;
    pc.hidden = false;
    const rows = [
      ["Entrada", money(p.entry_price) + " · " + when(p.entry_time)],
      ["Quantidade", num(p.qty, 6) + " BTC (" + money(p.notional) + ")"],
      ["Stop-loss", money(p.stop)],
      ["Alvo", money(p.take)],
      ["Risco máximo", money(p.risk_amount)],
      ["Resultado agora", signed(p.unrealized)],
    ];
    $("position").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  } else {
    pc.hidden = true;
  }

  // botão pausar
  const bp = $("btn-pause");
  bp.textContent = st.paused ? "▶️ Retomar robô" : "⏸️ Pausar robô";
  bp.className = "btn big" + (st.paused ? " resume" : "");

  // telegram
  $("tg-status").textContent = st.telegram_configurado
    ? "Telegram configurado. Alertas de sinais, operações e erros são enviados automaticamente."
    : "Telegram não configurado. Siga o passo a passo do README e depois use os botões abaixo.";

  // regras
  const p = st.params;
  const rules = [
    ["Saldo inicial", money(st.initial_balance)],
    ["Risco por operação", num(p.risk_per_trade * 100, 1) + "% do saldo"],
    ["Perda máxima no dia", num(p.daily_loss_limit * 100, 1) + "% do saldo inicial (" + money(st.daily_loss_cap) + ")"],
    ["Stop-loss", num(p.atr_stop_mult, 1) + "× ATR(14) abaixo da entrada"],
    ["Alvo", num(p.take_profit_r, 1) + "× a distância do stop"],
    ["Taxa simulada", num(p.fee_rate * 100, 2) + "% por execução"],
    ["Slippage simulado", num(p.slippage_rate * 100, 2) + "% por execução"],
    ["Erros nas últimas 24 h", String(st.erros_24h)],
  ];
  $("rules").replaceChildren(...rules.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
}

function checkItem(c) {
  return el("li", {}, el("span", { text: c.ok ? "✅" : "❌", "aria-label": c.ok ? "atendida" : "não atendida" }),
    el("span", { text: c.label }), el("span", { class: "val", text: c.value }));
}

/* ---------------------------------------------------------- gráficos */
let chart, candleSeries, emaFast, emaSlow, volSeries, equityChart, equitySeries, priceLines = [];
let currentTf = "5m";
let chartLabels = ["EMA9", "EMA21"];
let lastCandle = null;

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function chartTheme() {
  return {
    layout: { background: { color: cssVar("--surface") }, textColor: cssVar("--muted"), fontFamily: "system-ui, -apple-system, sans-serif" },
    grid: { vertLines: { color: cssVar("--grid") }, horzLines: { color: cssVar("--grid") } },
    rightPriceScale: { borderColor: cssVar("--axis") },
    timeScale: { borderColor: cssVar("--axis"), timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  };
}

function initCharts() {
  if (!window.LightweightCharts) return;
  chart = LightweightCharts.createChart($("chart"), { ...chartTheme(), autoSize: true, localization: { locale: "pt-BR", priceFormatter: (p) => num(p) } });
  candleSeries = chart.addCandlestickSeries({
    upColor: cssVar("--up"), downColor: cssVar("--down"), borderVisible: false,
    wickUpColor: cssVar("--up"), wickDownColor: cssVar("--down"),
  });
  volSeries = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  emaFast = chart.addLineSeries({ color: cssVar("--series-1"), lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
  emaSlow = chart.addLineSeries({ color: cssVar("--series-2"), lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
  chart.subscribeCrosshairMove((param) => {
    const bar = param && param.time ? param.seriesData.get(candleSeries) : null;
    const f = param && param.time ? param.seriesData.get(emaFast) : null;
    const s = param && param.time ? param.seriesData.get(emaSlow) : null;
    renderLegend(bar || lastCandle, f ? f.value : null, s ? s.value : null);
  });

  equityChart = LightweightCharts.createChart($("equity-chart"), { ...chartTheme(), autoSize: true, localization: { locale: "pt-BR", priceFormatter: (p) => num(p) } });
  equitySeries = equityChart.addAreaSeries({
    lineColor: cssVar("--series-1"), lineWidth: 2,
    topColor: cssVar("--series-1") + "33", bottomColor: cssVar("--series-1") + "00",
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  equityChart.subscribeCrosshairMove((param) => {
    const v = param && param.time ? param.seriesData.get(equitySeries) : null;
    $("equity-legend").textContent = v ? `${new Date((param.time - TZ_SHIFT) * 1000).toLocaleString("pt-BR")} · ${money(v.value)}` : "";
  });

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    chart.applyOptions(chartTheme());
    equityChart.applyOptions(chartTheme());
    emaFast.applyOptions({ color: cssVar("--series-1") });
    emaSlow.applyOptions({ color: cssVar("--series-2") });
    candleSeries.applyOptions({ upColor: cssVar("--up"), downColor: cssVar("--down"), wickUpColor: cssVar("--up"), wickDownColor: cssVar("--down") });
  });
}

function renderLegend(bar, fast, slow) {
  const lg = $("chart-legend");
  lg.replaceChildren();
  if (bar) {
    lg.append(el("span", { text: `A ${num(bar.open)}  M ${num(bar.high)}  m ${num(bar.low)}  F ${num(bar.close)}` }));
  }
  const key = (label, color, v) => el("span", { class: "key" }, el("span", { class: "swatch", style: `background:${color}` }), `${label} ${v === null ? "" : num(v)}`);
  lg.append(key(chartLabels[0], cssVar("--series-1"), fast), key(chartLabels[1], cssVar("--series-2"), slow));
}

async function refreshChart() {
  if (!chart) return;
  const data = await api("/api/chart?tf=" + currentTf);
  chartLabels = data.labels || chartLabels;
  const candles = data.candles.map((c) => ({ ...c, time: local(c.time) }));
  candleSeries.setData(candles);
  emaFast.setData(data.ema_fast.map((p) => ({ ...p, time: local(p.time) })));
  emaSlow.setData(data.ema_slow.map((p) => ({ ...p, time: local(p.time) })));
  const volColor = cssVar("--muted");
  volSeries.setData(data.volume.map((v) => ({ time: local(v.time), value: v.value, color: volColor + "55" })));
  lastCandle = candles[candles.length - 1] || null;
  renderLegend(lastCandle, null, null);

  // marcadores das operações simuladas (alinhados ao candle do gráfico)
  const step = currentTf === "5m" ? 300 : 900;
  const first = candles.length ? candles[0].time : Infinity;
  const markers = [];
  const snap = (ms) => local(Math.floor(ms / 1000 / step) * step);
  for (const t of data.trades) {
    const tIn = snap(t.entry_time);
    if (tIn >= first) markers.push({ time: tIn, position: "belowBar", color: cssVar("--up"), shape: "arrowUp", text: "C" });
    if (t.exit_time) {
      const tOut = snap(t.exit_time);
      if (tOut >= first) markers.push({ time: tOut, position: "aboveBar", color: cssVar("--down"), shape: "arrowDown", text: "V " + signed(t.pnl) });
    }
  }
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);

  for (const pl of priceLines) candleSeries.removePriceLine(pl);
  priceLines = [];
  const pos = lastStatus && lastStatus.position;
  if (pos) {
    const line = (price, title, color) => candleSeries.createPriceLine({ price, title, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true });
    priceLines = [line(pos.entry_price, "Entrada", cssVar("--muted")), line(pos.stop, "Stop", cssVar("--down")), line(pos.take, "Alvo", cssVar("--up"))];
  }
}

async function refreshEquity() {
  if (!equitySeries) return;
  const data = await api("/api/equity?days=30");
  $("equity-empty").hidden = data.length > 0;
  equitySeries.setData(data.map((p) => ({ time: local(p.time), value: p.value })));
}

/* ------------------------------------------------------- histórico */
async function refreshTrades() {
  const trades = await api("/api/trades?limit=50");
  const box = $("trades");
  if (!trades.length) { box.replaceChildren(el("p", { class: "muted", text: "Nenhuma operação ainda." })); return; }
  box.replaceChildren(...trades.map((t) => {
    const open = t.status === "open";
    const res = open ? "em aberto" : `${signed(t.pnl)} (${pct(t.pnl_pct)})`;
    return el("div", { class: "trade" },
      el("div", { text: `${money(t.entry_price)} → ${open ? "…" : money(t.exit_price)}` }),
      el("div", { class: "res " + (open ? "" : tone(t.pnl)), text: res }),
      el("div", { class: "meta", text: `${when(t.entry_time)}${t.exit_time ? " até " + when(t.exit_time) : ""} · ${num(t.qty, 6)} BTC` }),
      el("div", { class: "meta", text: open ? "" : (t.exit_reason || "") }),
      el("div", { class: "why", text: "Entrada: " + (t.reason || "") }));
  }));
}

async function refreshEvents() {
  const events = await api("/api/events?limit=60");
  const icon = { info: "ℹ️", aviso: "⚠️", erro: "❗" };
  $("events").replaceChildren(...events.map((e) =>
    el("li", {}, el("span", { text: icon[e.level] || "•" }), el("span", { text: e.message }), el("time", { text: when(e.time_ms) }))));
}

/* --------------------------------------------------------- backtest */
let btPolling = null;

function metricRows(m) {
  return [
    ["Saldo final", money(m.final_equity)],
    ["Retorno", pct(m.return_pct)],
    ["Comprar e segurar BTC", pct(m.buy_hold_pct)],
    ["Operações", `${m.trades} (${m.wins} ganhos / ${m.losses} perdas)`],
    ["Taxa de acerto", m.win_rate === null ? "—" : num(m.win_rate, 1) + "%"],
    ["Fator de lucro", m.profit_factor === null ? "—" : num(m.profit_factor, 2)],
    ["Drawdown máximo", num(m.max_drawdown_pct, 2) + "%"],
    ["Taxas pagas", money(m.fees_paid)],
    ["Resultado antes dos custos", signed(m.gross_pnl)],
    ["Custos (taxas + slippage)", money(m.costs)],
    ["Saídas", Object.entries(m.exit_reasons || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "—"],
    ["Dias travados (−3%)", String(m.daily_halts)],
  ];
}

function btPart(title, part) {
  const table = el("table", { class: "bt-table" },
    el("tbody", {}, ...metricRows(part.metricas).map(([k, v]) => el("tr", {}, el("td", { text: k }), el("td", { text: v })))));
  return el("div", { class: "bt-part" },
    el("h3", { text: title }),
    el("p", { class: "muted small", text: `${part.inicio} → ${part.fim} · stop ${part.parametros.atr_stop_mult}× ATR · alvo ${part.parametros.take_profit_r}× risco` }),
    el("div", { class: "bt-scroll" }, table));
}

function renderBacktest(data) {
  const box = $("backtest");
  const btn = $("btn-backtest");
  btn.disabled = data.running;
  btn.textContent = data.running ? "Rodando…" : "Rodar backtest";
  const nodes = [];
  if (data.running) nodes.push(el("p", { class: "small", text: "⏳ Baixando dados e testando… pode levar 1–3 minutos." }));
  if (data.error) nodes.push(el("p", { class: "small neg", text: "Falhou: " + data.error }));
  const r = data.report;
  if (r) {
    nodes.push(el("p", { class: "small muted", text: `Último backtest: ${r.gerado_em} · fonte ${r.fonte_de_dados} · ${r.dias} dias · taxa ${num(r.custos.taxa_por_execucao_pct, 2)}% + slippage ${num(r.custos.slippage_pct, 2)}% por execução` }));
    nodes.push(btPart("Parte 2 — dados novos (fora da amostra)", r.parte2_fora_da_amostra));
    if (r.parte2_sem_custos) nodes.push(el("p", { class: "small muted", text: `Diagnóstico: sem taxas nem slippage a parte 2 teria dado ${pct(r.parte2_sem_custos.retorno_pct)} — mostra o peso dos custos (não é um resultado possível).` }));
    nodes.push(btPart("Parte 1 — usada para escolher parâmetros", r.parte1_escolhido));
    nodes.push(el("ul", { class: "small" }, ...r.avisos.map((a) => el("li", { text: a }))));
  } else if (!data.running) {
    nodes.push(el("p", { class: "muted small", text: "Nenhum backtest rodado ainda." }));
  }
  box.replaceChildren(...nodes);
  if (data.running && !btPolling) btPolling = setInterval(refreshBacktest, 5000);
  if (!data.running && btPolling) { clearInterval(btPolling); btPolling = null; }
}

async function refreshBacktest() { renderBacktest(await api("/api/backtest")); }

/* ---------------------------------------------------------- ações */
function bind() {
  $("btn-pause").addEventListener("click", async () => {
    const pausing = !(lastStatus && lastStatus.paused);
    if (pausing && !confirm("Pausar o robô? Ele não abrirá novas operações (a posição aberta continua com stop e alvo).")) return;
    try {
      await api(pausing ? "/api/pause" : "/api/resume", { method: "POST" });
      toast(pausing ? "Robô pausado." : "Robô retomado.");
      await refreshStatus();
    } catch (e) { toast(e.message); }
  });
  $("btn-close").addEventListener("click", async () => {
    if (!confirm("Encerrar a posição simulada agora, ao preço atual?")) return;
    try {
      const r = await api("/api/close-position", { method: "POST" });
      toast(r.message);
      await refreshAll();
    } catch (e) { toast(e.message); }
  });
  $("btn-backtest").addEventListener("click", async () => {
    try {
      const r = await api("/api/backtest?days=" + $("bt-days").value, { method: "POST" });
      toast(r.message);
      await refreshBacktest();
    } catch (e) { toast(e.message); }
  });
  $("btn-tg-test").addEventListener("click", async () => {
    try {
      const r = await api("/api/telegram/test", { method: "POST" });
      $("tg-result").textContent = r.message;
    } catch (e) { $("tg-result").textContent = e.message; }
  });
  $("btn-tg-chats").addEventListener("click", async () => {
    try {
      const r = await api("/api/telegram/chats");
      const box = $("tg-result");
      box.replaceChildren(el("p", { text: r.message }),
        ...r.chats.map((c) => el("p", {}, el("strong", { text: "chat_id: " + c.chat_id }), ` — ${c.nome || ""} (${c.tipo || ""})`)));
    } catch (e) { $("tg-result").textContent = e.message; }
  });
  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("active", x === b));
    currentTf = b.dataset.tf;
    refreshChart().catch(() => {});
  }));
}

async function refreshStatus() { renderStatus(await api("/api/status")); }

async function refreshAll() {
  try { await refreshStatus(); } catch (e) { if (e.message !== "login") toast("Sem conexão com o robô: " + e.message); return; }
  await Promise.allSettled([refreshChart(), refreshEquity(), refreshTrades(), refreshEvents()]);
}

bind();
initCharts();
refreshAll();
refreshBacktest().catch(() => {});
setInterval(() => { if (!document.hidden) refreshAll(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
