"use strict";

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let TZ_LABEL = "GMT-4 (Manaus)";

// The engine emits the canonical English 5-tier rating (Buy / Overweight / Hold
// / Underweight / Sell). On screen we show the *practical meaning* in pt-BR —
// what to actually do — and keep the original jargon in gray beside it for
// whoever knows the scale. Keyed by the rating lowercased with non-letters
// stripped, matching verdictClass()/verdictKey().
const VERDICT_PT = {
  buy: "COMPRAR",
  overweight: "AUMENTAR",
  hold: "MANTER",
  underweight: "REDUZIR",
  sell: "VENDER",
};

// Non-verdict run statuses that can reach the history chip (verdict is null on
// a failed run), so nothing shows up in English there either.
const STATUS_PT = { error: "ERRO", running: "RODANDO", done: "CONCLUÍDO" };

// The engine tags each run with an asset class in English ("stock"/"crypto").
// Show it in pt-BR so the meta row carries no stray English.
const ASSET_PT = { stock: "ação", crypto: "cripto" };
function assetPt(t) {
  const k = (t || "").toString().toLowerCase();
  return ASSET_PT[k] || t || "";
}

// Setup state emitted by the deterministic actionable plan -> emoji anchor + pt-BR
// label. Keeps the header's "what to do now" legible at a glance (DA-034).
const SETUP_PT = {
  ativo: ["🎯", "Setup ativo agora"],
  aguardar_pullback: ["⏳", "Aguardar recuo à média"],
  aguardar_rompimento: ["⏳", "Aguardar rompimento"],
  sem_setup: ["⚪", "Sem setup de preço definido"],
  sem_dado: ["⚪", "Sem dado suficiente"],
};

function verdictKey(v) {
  return (v || "").toLowerCase().replace(/[^a-z]/g, "");
}

// Practical pt-BR label + the original English rating in gray beside it.
function verdictHtml(v) {
  const key = verdictKey(v);
  const raw = (v || "").toString();
  if (VERDICT_PT[key]) return `${VERDICT_PT[key]}<span class="verdict-orig">${escapeHtml(raw)}</span>`;
  if (STATUS_PT[key]) return escapeHtml(STATUS_PT[key]);
  return escapeHtml((raw || "—").toUpperCase());
}

// Format a Manaus ISO stamp ("2026-08-23T20:30:00-04:00") for display WITHOUT
// going through Date() — the string already carries Manaus wall time, so we read
// it verbatim and never let the browser's timezone re-shift it. Returns e.g.
// "23/08 20:30" (or with the tz label appended when withTz is set).
function fmtStamp(iso, withTz) {
  if (!iso) return "";
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return escapeHtml(String(iso));
  const [, , mo, d, hh, mm] = m;
  const base = `${d}/${mo} ${hh}:${mm}`;
  return withTz ? `${base} ${TZ_LABEL}` : base;
}

// ---- tiny markdown renderer (no external deps; offline-safe) --------------
function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function inline(s) {
  s = escapeHtml(s);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // highlight the engine's own "unavailable / indisponível" markers
  s = s.replace(/\b(N\/A|unavailable|indispon[ií]vel)\b/gi, '<span class="unavailable">$1</span>');
  return s;
}

function renderMarkdown(text) {
  if (!text || !text.trim()) return '<p class="unavailable">indisponível</p>';
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let i = 0;
  let listType = null;
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };

  while (i < lines.length) {
    let line = lines[i];

    // fenced code block
    if (/^```/.test(line)) {
      closeList();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      html += `<pre><code>${escapeHtml(buf.join("\n"))}</code></pre>`;
      continue;
    }

    // table: header row + separator row of ---|---
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      closeList();
      const parseRow = (r) => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
      const headers = parseRow(line);
      i += 2;
      let t = "<table><thead><tr>" + headers.map((h) => `<th>${inline(h)}</th>`).join("") + "</tr></thead><tbody>";
      while (i < lines.length && lines[i].includes("|")) {
        const cells = parseRow(lines[i]);
        t += "<tr>" + cells.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>";
        i++;
      }
      t += "</tbody></table>";
      html += t;
      continue;
    }

    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      closeList();
      const lvl = m[1].length;
      html += `<h${lvl}>${inline(m[2])}</h${lvl}>`;
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      closeList();
      html += "<hr>";
    } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${inline(m[1])}</li>`;
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      html += `<p>${inline(line)}</p>`;
    }
    i++;
  }
  closeList();
  return html;
}

// ---- rendering ------------------------------------------------------------
function verdictClass(v) {
  return "verdict " + (v || "").toLowerCase().replace(/[^a-z]/g, "");
}

function fmtCost(cost) {
  if (!cost) return "$0.0000";
  const usd = typeof cost === "number" ? cost : cost.usd || 0;
  const partial = cost && cost.complete === false ? "*" : "";
  return "$" + usd.toFixed(4) + partial;
}

function renderProgress(snap) {
  $("progressPanel").classList.remove("hidden");
  const p = snap.progress || {};
  $("progressPhase").textContent = p.phase || "…";
  $("progressLabel").textContent = p.label || "";
  $("progressElapsed").textContent = (snap.elapsed || 0) + "s";
  $("progressCost").textContent = fmtCost(snap.cost);
  $("barFill").style.width = (p.percent || 0) + "%";

  const steps = $("steps");
  if (p.plan && p.plan.length && steps.childElementCount !== p.plan.length) {
    steps.innerHTML = p.plan.map((s) => `<li data-label="${escapeHtml(s.label)}">${escapeHtml(s.label.split(" — ")[0])}</li>`).join("");
  }
  const reachedLabels = new Set((p.reached || []).map((r) => r.label));
  const activeLabel = p.label;
  [...steps.children].forEach((li) => {
    const label = li.getAttribute("data-label");
    li.classList.toggle("done", reachedLabels.has(label) && label !== activeLabel);
    li.classList.toggle("active", label === activeLabel && snap.status === "running");
  });
}

function section(title, mdText) {
  if (!mdText || !mdText.trim()) return "";
  return `<details class="section"><summary>${escapeHtml(title)}</summary>` +
    `<div class="section-body"><div class="md">${renderMarkdown(mdText)}</div></div></details>`;
}

function renderResult(snap) {
  const nameEl = document.getElementById("assetName");
  if (nameEl) nameEl.textContent = snap.ticker || "—";
  _openTicker = snap.ticker || "";
  const rb = document.getElementById("refreshBtn");
  if (rb) rb.classList.toggle("hidden", !_openTicker);
  renderAssetTimeline(_openTicker, snap.run_id);
  clearInterval(pollTimer); pollTimer = null;
  $("runBtn").disabled = false;
  $("progressPanel").classList.add("hidden");
  const panel = $("resultPanel");
  panel.classList.remove("hidden");

  if (snap.status === "error") {
    $("chartCard").classList.add("hidden");
    $("actionable").classList.add("hidden");
    $("verdictBadge").className = "verdict sell";
    $("verdictBadge").textContent = "ERRO";
    $("resultMeta").innerHTML = `<span>${escapeHtml(snap.error || "falha")}</span>`;
    $("bull").innerHTML = ""; $("bear").innerHTML = "";
    $("sections").innerHTML = section("Rastreamento do erro", "```\n" + ((snap.result && snap.result.trace) || "") + "\n```");
    return;
  }

  const r = snap.result || {};
  $("verdictBadge").className = verdictClass(r.verdict);
  $("verdictBadge").innerHTML = verdictHtml(r.verdict);
  const finished = snap.finished_at || (snap.result && snap.result.finished_at);
  $("resultMeta").innerHTML =

    `<span>Data da análise <b>${escapeHtml(snap.date || "")}</b></span>` +
    `<span>Tipo <b>${escapeHtml(assetPt(snap.asset_type))}</b></span>` +
    `<span>Custo <b>${fmtCost(snap.cost)}</b></span>` +
    `<span>Tempo <b>${snap.elapsed || 0}s</b></span>` +
    (finished ? `<span>Concluído <b>${fmtStamp(finished, true)}</b></span>` : "");

  renderActionable(r.actionable);
  renderChartCard(r.price_chart, snap.ticker);

  $("bull").innerHTML = renderMarkdown(r.bull);
  $("bear").innerHTML = renderMarkdown(r.bear);

  const isCrypto = snap.asset_type === "crypto";
  let html = "";
  // For crypto, the deterministic derivatives feed goes first and open — it is
  // the data yfinance can't see and the source is always named here.
  if (isCrypto && r.derivatives_report && r.derivatives_report.trim()) {
    html += `<details class="section" open><summary>🪙 Derivativos — taxa de financiamento <span class="orig">(funding)</span> · contratos em aberto <span class="orig">(open interest)</span> · liquidações <span class="orig">(liquidations)</span> (fonte nomeada)</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.derivatives_report)}</div></div></details>`;
  }
  html += section("⚖️ Juiz do Debate (Gestor de Pesquisa)", r.research_manager || r.investment_plan);
  html += section("📊 Mercado — preço e múltiplos tempos gráficos", r.market_report);
  html += section("📰 Notícias — macro e mercados de previsão", r.news_report);
  html += section("💬 Sentimento", r.sentiment_report);
  if (!isCrypto) html += section("📑 Fundamentos", r.fundamentals_report);
  html += section("🎯 Plano do Trader", r.trader_plan);
  html += section("🛡️ Decisão de Risco (veredito final na íntegra)", r.risk_decision || r.final_trade_decision);
  $("sections").innerHTML = html;

  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  loadHistory();
}

// ---- actionable plan (operable levels beside the verdict) -----------------
// A level is shown only when the engine derived it from the real series; when
// there is no basis we print "sem nível definido" — never a fabricated number.
function zoneHtml(zone, emptyText) {
  if (!zone || zone.price == null) {
    return `<span class="nolevel">${escapeHtml(emptyText || "sem nível definido")}</span>`;
  }
  const label = zone.label ? ` <span class="zlabel">· ${escapeHtml(zone.label)}</span>` : "";
  return `<b>${fmtNum(zone.price)}</b>${label}`;
}

function actRow(emoji, key, valHtml) {
  return `<div class="act-row"><span class="act-k">${emoji} ${escapeHtml(key)}</span>` +
    `<span class="act-v">${valHtml}</span></div>`;
}

function renderActionable(a) {
  const el = $("actionable");
  if (!a || !a.setup_state) { el.classList.add("hidden"); return; }

  const [emo, label] = SETUP_PT[a.setup_state] || ["⚪", a.setup_state];
  const priceHtml = a.price != null
    ? `<b>${fmtNum(a.price)}</b>${a.as_of ? ` <span class="zlabel">em ${escapeHtml(a.as_of)}</span>` : ""}`
    : `<span class="nolevel">indisponível</span>`;
  // When the setup is live the price is already at the buy region, so there is
  // no recuo to wait for — say so instead of "sem nível definido".
  const pullbackEmpty = a.setup_state === "ativo" ? "já na região (setup ativo)" : "sem nível definido";

  el.innerHTML =
    `<div class="act-head"><span class="act-title">Plano acionável</span>` +
    `<span class="act-setup ${escapeHtml(a.setup_state)}">${emo} ${escapeHtml(label)}</span></div>` +
    `<div class="act-grid">` +
    actRow("💰", "Preço na análise", priceHtml) +
    actRow("🕐", "Horizonte", escapeHtml(a.horizon || "—")) +
    actRow("📐", "Timeframe", escapeHtml(a.timeframe || "—")) +
    actRow("🟢", "Região de compra", zoneHtml(a.buy_zone)) +
    actRow("🎯", "Região de realização", zoneHtml(a.realize_zone)) +
    actRow("⏳", "Pullback a aguardar", zoneHtml(a.pullback_zone, pullbackEmpty)) +
    `</div>`;
  el.classList.remove("hidden");
}

// ---- candlestick chart (canvas, no external deps) -------------------------
// Moving-average colours, keyed by window. Chosen to stay legible on the dark
// panel and distinct from the green/red candles.
const MA_COLORS = { "20": "#f5b445", "50": "#6ea8fe", "200": "#b48ef5" };
let _lastChart = null; // kept so a window resize can redraw crisply.

function renderChartCard(chart, ticker) {
  const card = $("chartCard");
  const hasData = chart && Array.isArray(chart.candles) && chart.candles.length > 2;
  if (!hasData) { card.classList.add("hidden"); _lastChart = null; return; }
  card.classList.remove("hidden");
  _lastChart = chart;

  const active = chart.markers && chart.markers.active_region;
  const pat = chart.markers && chart.markers.pattern_123;
  card.classList.toggle("setup-active", !!active || (pat && pat.state === "acionado"));

  // legend: candles + each MA present + setup markers
  const wins = (chart.ma_windows || [20, 50, 200]).map(String);
  const legend = [
    `<span class="lg"><span class="sw" style="background:var(--green)"></span>vela de alta <span class="orig">(candle)</span></span>`,
    `<span class="lg"><span class="sw" style="background:var(--red)"></span>vela de baixa</span>`,
  ];
  wins.forEach((w) => {
    if (MA_COLORS[w]) legend.push(`<span class="lg"><span class="sw" style="background:${MA_COLORS[w]}"></span>MMS${w}</span>`);
  });
  legend.push(`<span class="lg"><span class="sw dot" style="background:#2ecc71"></span>região de compra</span>`);
  if (pat) legend.push(`<span class="lg"><span class="sw dot" style="background:#6ea8fe"></span>padrão 1-2-3</span>`);
  $("chartLegend").innerHTML = legend.join("");

  // note: pt-BR summary of what's marked
  const notes = [];
  if (active) {
    notes.push(`🎯 <b>Setup ativo agora</b>: preço na ${active.ma_label} (${Math.abs(active.distance_pct).toFixed(1)}% ${active.distance_pct >= 0 ? "acima" : "abaixo"}).`);
  }
  const nreg = (chart.markers && chart.markers.buy_regions || []).length;
  if (nreg) notes.push(`${nreg} região(ões) de compra na média marcada(s) no período.`);
  if (pat) notes.push(`Padrão 1-2-3: gatilho em ${fmtNum(pat.trigger)} — <b>${pat.state}</b>.`);
  if (!notes.length) notes.push("Nenhum setup identificado na janela do gráfico.");
  $("chartNote").innerHTML = notes.join(" ");

  drawPriceChart($("priceChart"), chart);
}

function fmtNum(v) {
  return (typeof v === "number" ? v : Number(v)).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function drawPriceChart(canvas, chart) {
  const candles = chart.candles;
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.parentElement.clientWidth || 640;
  const cssH = canvas.clientHeight || 380;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const padL = 8, padR = 58, padT = 12, padB = 22;
  const plotW = cssW - padL - padR, plotH = cssH - padT - padB;
  const n = candles.length;

  // price range across candles + MAs + pattern levels, with a little padding
  let lo = Infinity, hi = -Infinity;
  candles.forEach((c) => { if (c.l != null) lo = Math.min(lo, c.l); if (c.h != null) hi = Math.max(hi, c.h); });
  Object.values(chart.ma || {}).forEach((arr) => arr.forEach((v) => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
  const pat = chart.markers && chart.markers.pattern_123;
  if (pat) [pat.p1.price, pat.p2.price, pat.p3.price, pat.trigger].forEach((v) => { lo = Math.min(lo, v); hi = Math.max(hi, v); });
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) { lo = 0; hi = 1; }
  const pad = (hi - lo) * 0.05; lo -= pad; hi += pad;

  const x = (i) => padL + (i + 0.5) * (plotW / n);
  const y = (p) => padT + (1 - (p - lo) / (hi - lo)) * plotH;

  // gridlines + price labels (y axis, right)
  ctx.font = "11px ui-monospace, Menlo, monospace";
  ctx.textBaseline = "middle";
  const ticks = 5;
  for (let t = 0; t <= ticks; t++) {
    const p = lo + (hi - lo) * (t / ticks);
    const yy = y(p);
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
    ctx.fillStyle = "#8b97ad"; ctx.textAlign = "left";
    ctx.fillText(p.toLocaleString("pt-BR", { maximumFractionDigits: p < 10 ? 2 : 0 }), padL + plotW + 6, yy);
  }

  // date labels (x axis) — a handful, evenly spaced
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  const labels = 5;
  for (let t = 0; t <= labels; t++) {
    const i = Math.min(n - 1, Math.round((n - 1) * (t / labels)));
    const d = candles[i].d; // YYYY-MM-DD -> DD/MM
    ctx.fillStyle = "#8b97ad";
    ctx.fillText(d.slice(8, 10) + "/" + d.slice(5, 7), x(i), padT + plotH + 5);
  }

  // candles
  const cw = Math.max(1, Math.min((plotW / n) * 0.7, 9));
  candles.forEach((c, i) => {
    if (c.o == null || c.c == null) return;
    const up = c.c >= c.o;
    const col = up ? "#2ecc71" : "#ff5c6c";
    ctx.strokeStyle = col; ctx.fillStyle = col;
    const cx = x(i);
    ctx.beginPath(); ctx.moveTo(cx, y(c.h)); ctx.lineTo(cx, y(c.l)); ctx.stroke();
    const yo = y(c.o), yc = y(c.c);
    const top = Math.min(yo, yc), h = Math.max(1, Math.abs(yc - yo));
    ctx.fillRect(cx - cw / 2, top, cw, h);
  });

  // moving-average polylines
  Object.entries(chart.ma || {}).forEach(([w, arr]) => {
    const color = MA_COLORS[w]; if (!color) return;
    ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath();
    let started = false;
    arr.forEach((v, i) => {
      if (v == null) { started = false; return; }
      const px = x(i), py = y(v);
      if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
    });
    ctx.stroke(); ctx.lineWidth = 1;
  });

  // date -> index map for markers
  const idx = {}; candles.forEach((c, i) => { idx[c.d] = i; });

  // buy-region dots (at the candle low)
  const regions = (chart.markers && chart.markers.buy_regions) || [];
  regions.forEach((r) => {
    const i = idx[r.date]; if (i == null) return;
    const px = x(i), py = y(candles[i].l) + 7;
    ctx.fillStyle = "#2ecc71";
    ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2); ctx.fill();
  });

  // 1-2-3 pattern: connecting line, labelled points, trigger level
  if (pat) {
    const pts = [["1", pat.p1, "L"], ["2", pat.p2, "H"], ["3", pat.p3, "L"]]
      .map(([lab, p, kind]) => ({ lab, kind, i: idx[p.date], price: p.price }))
      .filter((p) => p.i != null);
    if (pts.length) {
      ctx.strokeStyle = "#6ea8fe"; ctx.setLineDash([4, 3]); ctx.lineWidth = 1.5;
      ctx.beginPath();
      pts.forEach((p, k) => { const px = x(p.i), py = y(p.price); k ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
      ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
      // trigger horizontal line
      const ty = y(pat.trigger);
      ctx.strokeStyle = "rgba(110,168,254,0.5)"; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(padL, ty); ctx.lineTo(padL + plotW, ty); ctx.stroke(); ctx.setLineDash([]);
      // point labels
      ctx.fillStyle = "#6ea8fe"; ctx.font = "bold 12px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      pts.forEach((p) => {
        const px = x(p.i), py = y(p.price) + (p.kind === "L" ? 14 : -14);
        ctx.fillStyle = "#0b0e14"; ctx.beginPath(); ctx.arc(px, py, 8, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = "#6ea8fe"; ctx.stroke();
        ctx.fillStyle = "#6ea8fe"; ctx.fillText(p.lab, px, py);
      });
    }
  }
}

// redraw on resize so the canvas stays crisp and correctly scaled
let _resizeTimer = null;
window.addEventListener("resize", () => {
  if (!_lastChart) return;
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => drawPriceChart($("priceChart"), _lastChart), 150);
});

// ---- polling & actions ----------------------------------------------------
async function poll(runId) {
  try {
    const res = await fetch("/api/status/" + runId);
    if (!res.ok) throw new Error("status " + res.status);
    const snap = await res.json();
    if (snap.status === "running") {
      renderProgress(snap);
    } else {
      renderResult(snap);
    }
  } catch (e) {
    // transient error — keep polling
  }
}

async function startAnalysis(ev) {
  ev.preventDefault();
  $("formError").textContent = "";
  const ticker = $("ticker").value.trim();
  const date = $("date").value;
  if (!ticker) { $("formError").textContent = "Informe um ticker."; return; }
  $("runBtn").disabled = true;
  $("resultPanel").classList.add("hidden");
  $("steps").innerHTML = "";
  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, date }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "falha ao iniciar");
    renderProgress({ status: "running", elapsed: 0, cost: null, progress: { phase: "Inicializando", label: "Subindo o motor…", percent: 2, plan: [], reached: [] } });
    poll(data.run_id);
    pollTimer = setInterval(() => poll(data.run_id), 1500);
  } catch (e) {
    $("runBtn").disabled = false;
    $("formError").textContent = e.message;
  }
}

let _openTicker = "";

// Calendário do ativo: as análises anteriores DESTE ticker, do mais recente pro
// mais antigo. Substitui a repetição do mesmo ativo na lista lateral.
function renderAssetTimeline(ticker, currentId) {
  const el = document.getElementById("assetTimeline");
  if (!el) return;
  const runs = (_allRuns || []).filter(
    (r) => (r.ticker || "").toUpperCase() === (ticker || "").toUpperCase()
  );
  if (runs.length < 2) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");

  // Grade estilo contribuições: 7 linhas (dom→sáb) × semanas. Uma faixa de
  // botões por dia não cabe depois de algumas semanas de uso; a grade põe ~3
  // meses num espaço fixo e o dia sem análise fica apagado.
  const WEEKS = 13;
  const byDay = new Map();          // "YYYY-MM-DD" -> run mais recente do dia
  runs.forEach((r) => { if (r.date && !byDay.has(r.date)) byDay.set(r.date, r); });

  const today = new Date(`${_todayManaus || runs[0].date}T12:00:00`);
  const end = new Date(today);
  end.setDate(end.getDate() + (6 - end.getDay()));   // fecha a semana corrente
  const cells = [];
  for (let i = WEEKS * 7 - 1; i >= 0; i--) {
    const d = new Date(end);
    d.setDate(d.getDate() - i);
    cells.push(d.toISOString().slice(0, 10));
  }

  const cols = [];
  for (let w = 0; w < WEEKS; w++) {
    const days = cells.slice(w * 7, w * 7 + 7).map((iso) => {
      const r = byDay.get(iso);
      if (!r) return `<span class="cal-cell is-empty" title="${iso}"></span>`;
      const v = (r.verdict || r.status || "").toString();
      const cur = r.run_id === currentId ? " is-current" : "";
      return `<button type="button" class="cal-cell${cur} ${verdictClass(v).replace("verdict", "").trim()}" ` +
        `data-id="${escapeHtml(r.run_id)}" title="${iso} — ${escapeHtml(v)}"></button>`;
    }).join("");
    cols.push(`<div class="cal-col">${days}</div>`);
  }

  const first = cells[0].slice(5), last = cells[cells.length - 1].slice(5);
  el.innerHTML = `<span class="tl-label">Análises anteriores</span>` +
    `<div class="cal-grid">${cols.join("")}</div>` +
    `<span class="cal-range">${first} → ${last} · ${byDay.size} dia(s)</span>`;
  el.querySelectorAll("button.cal-cell").forEach((b) =>
    b.addEventListener("click", () => openRun(b.dataset.id))
  );
}

let _todayManaus = "";
let _historyFilter = "all";

// Reanalisa o ativo que está aberto, na data de hoje (Manaus, vinda do servidor).
// Preenche o formulário e dispara o mesmo caminho da análise manual — nenhum
// fluxo paralelo, pra não divergir do que o botão principal faz.
function bindRefresh() {
  const rb = document.getElementById("refreshBtn");
  if (!rb) return;
  rb.addEventListener("click", () => {
    if (!_openTicker) return;
    $("ticker").value = _openTicker;
    if (_todayManaus) $("date").value = _todayManaus;
    $("analyzeForm").requestSubmit
      ? $("analyzeForm").requestSubmit()
      : startAnalysis(new Event("submit"));
  });
}
let _allRuns = [];

function bindHistoryTabs() {
  const tabs = document.getElementById("historyTabs");
  if (!tabs) return;
  tabs.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".h-tab");
    if (!btn) return;
    _historyFilter = btn.dataset.filter || "all";
    tabs.querySelectorAll(".h-tab").forEach((b) => b.classList.toggle("is-active", b === btn));
    loadHistory();
  });
}

async function loadHistory() {
  try {
    const res = await fetch("/api/history");
    const data = await res.json();
    const ul = $("history");
    const runs = data.runs || [];
    if (!runs.length) { ul.innerHTML = '<li class="empty">Nenhuma análise ainda.</li>'; return; }
    // Duas famílias só: cripto e ações (metais entram em ações). O usuário
    // escolhe pela aba — Todos / Ações / Cripto —, sem cabeçalho empilhado.
    _allRuns = runs;
    // A lateral é a LISTA DE ATIVOS, não o log de execuções: um item por ticker,
    // com o veredito mais recente. O histórico daquele ativo (dias atrás) aparece
    // como calendário dentro da análise aberta.
    const item = (r, n) => {
      const v = (r.verdict || r.status || "").toString();
      const when = r.finished_at ? fmtStamp(r.finished_at) : escapeHtml(r.date || "");
      const badge = n > 1 ? `<span class="h-count" title="${n} análises">${n}</span>` : "";
      return `<li data-id="${escapeHtml(r.run_id)}">` +
        `<span class="h-ticker">${escapeHtml(r.ticker || "?")}${badge}</span>` +
        `<span class="h-verdict ${verdictClass(v).replace("verdict", "").trim()}" title="${escapeHtml(v)}">${verdictHtml(v)}</span>` +
        `<span class="h-meta">${when} · ${fmtCost({ usd: r.cost_usd || 0 })} · ${r.elapsed || 0}s</span>` +
        `</li>`;
    };
    const filtered = _historyFilter === "all"
      ? runs
      : runs.filter((r) => (r.asset_type === "crypto") === (_historyFilter === "crypto"));
    // um por ticker (o mais recente; a API já devolve do mais novo pro mais velho)
    const seen = new Map();
    filtered.forEach((r) => {
      const k = (r.ticker || "?").toUpperCase();
      if (!seen.has(k)) seen.set(k, { run: r, n: 0 });
      seen.get(k).n += 1;
    });
    ul.innerHTML = seen.size
      ? [...seen.values()].map(({ run, n }) => item(run, n)).join("")
      : '<li class="empty">Nenhuma análise nesta aba.</li>';
    [...ul.children].forEach((li) => {
      const id = li.getAttribute("data-id");
      if (id) li.addEventListener("click", () => openRun(id));
    });
  } catch (e) { /* ignore */ }
}

async function openRun(runId) {
  try {
    const res = await fetch("/api/run/" + runId);
    const snap = await res.json();
    if (res.ok) renderResult(snap);
  } catch (e) { /* ignore */ }
}

async function applyConfig() {
  // The authoritative "today" is Manaus-on-the-server, not the browser clock.
  try {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    if (cfg.tz_label) TZ_LABEL = cfg.tz_label;
    if (cfg.today) { _todayManaus = cfg.today; $("date").value = cfg.today; }
    $("tzLabel").textContent = "(" + TZ_LABEL + ")";
    $("tzNote").textContent = "Horários em " + TZ_LABEL + ".";
  } catch (e) {
    // fallback: browser-local date if the server is unreachable at boot
    $("date").value = new Date().toLocaleDateString("en-CA");
  }
}

function init() {
  applyConfig();
  $("analyzeForm").addEventListener("submit", startAnalysis);
  $("ticker").addEventListener("input", () => {
    const t = $("ticker").value.trim().toUpperCase();
    $("assetHint").innerHTML = /-(USD|USDT)$|^BTC|^ETH/.test(t) ? `Detectado: cripto — inclui taxa de financiamento <span class="orig">(funding)</span>, contratos em aberto <span class="orig">(open interest)</span> e liquidações <span class="orig">(liquidations)</span>.` : "";
  });
  $("netNote").textContent = "acesse por " + location.host;
  bindHistoryTabs();
  bindRefresh();
  loadHistory().then(openLatestRun);
}

// Ao abrir a página, mostra a análise mais recente em vez de tela vazia. Sem isso
// o usuário abre depois de um deploy, vê a lista e o formulário, e conclui que
// "nada mudou" — porque o card novo só aparece ao clicar num run.
async function openLatestRun() {
  try {
    const res = await fetch("/api/history");
    if (!res.ok) return;
    const runs = await res.json();
    const first = Array.isArray(runs) ? runs[0] : (runs.runs || [])[0];
    const id = first && (first.run_id || first.id);
    if (id) openRun(id);
  } catch (e) { /* histórico vazio ou offline: deixa a tela como está */ }
}

document.addEventListener("DOMContentLoaded", init);
