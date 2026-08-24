"use strict";

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let TZ_LABEL = "GMT-4 (Manaus)";

// Segundo plano: cada análise roda numa thread própria no servidor e continua
// mesmo se o usuário troca de ativo, sai da tela ou recarrega. Estes controlam
// só a VISÃO — qual run está sendo acompanhado ao vivo, quais estavam rodando na
// última atualização da lista, e quais terminaram sozinhos (ganham "pronto").
let _watchedRunId = "";              // run cujo progresso está na tela agora
let _openRunId = "";                 // run simples aberto (lado A de um confronto manual)
let _prevRunningIds = new Set();     // ids que apareciam "running" na última lista
const _finishedFlags = new Map();    // run_id -> "done" | "error" (terminou em 2º plano)
let _historyTimer = null;            // atualização lenta da lista (marcadores vivos)

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

// 1-2-3 direction/state -> emoji + pt-BR. Compra (fundo ascendente) e venda
// (topo descendente) recebem cor distinta no card e no gráfico (fork brief 24/08).
const PAT_DIR = {
  compra: ["🟢", "de compra", "fundo ascendente"],
  venda: ["🔴", "de venda", "topo descendente"],
};
const PAT_STATE = {
  acionado: ["✅", "acionado"],
  formando: ["⏳", "em formação"],
};

// "2025-08-14" -> "14/08" sem passar por Date() (evita re-shift de timezone).
function fmtDate(iso) {
  const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[3]}/${m[2]}` : escapeHtml(String(iso || ""));
}

function verdictKey(v) {
  return (v || "").toLowerCase().replace(/[^a-z]/g, "");
}

// Practical pt-BR label + the original English rating in gray beside it.
function verdictHtml(v) {
  const key = verdictKey(v);
  const raw = (v || "").toString();
  if (VERDICT_PT[key]) return `${VERDICT_PT[key]} <span class="verdict-orig">${escapeHtml(raw)}</span>`;
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
  const tk = $("progressTicker");
  if (tk) {
    // qual ativo está sendo analisado — some quando não sabemos o ticker (start
    // sintético antes do 1º poll já manda o ticker, então quase sempre aparece)
    tk.textContent = snap.ticker || "";
    tk.classList.toggle("hidden", !snap.ticker);
  }
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
  // este run passa a ser o "aberto" na tela: enquanto for ele, um término dele
  // NÃO vira aviso "pronto" (o usuário já está vendo o resultado).
  _watchedRunId = snap.run_id || _watchedRunId;
  $("comparePanel").classList.add("hidden");
  // Run de comparação (Padrão × Erick): view própria, lado a lado.
  if ((snap.result || {}).compare) { renderCompare(snap); return; }
  const nameEl = document.getElementById("assetName");
  if (nameEl) nameEl.textContent = snap.ticker || "—";
  _openTicker = snap.ticker || "";
  _openRunId = snap.run_id || "";
  const rb = document.getElementById("refreshBtn");
  if (rb) rb.classList.toggle("hidden", !_openTicker);
  renderAssetTimeline(_openTicker, snap.run_id);
  renderConfrontControl(snap);
  clearInterval(pollTimer); pollTimer = null;
  $("runBtn").disabled = false;
  $("progressPanel").classList.add("hidden");
  const panel = $("resultPanel");
  panel.classList.remove("hidden");

  if (snap.status === "error") {
    $("chartCard").classList.add("hidden");
    $("actionable").classList.add("hidden");
    $("headPrice").classList.add("hidden");
    $("verdictTf").classList.add("hidden");
    $("degradedBanner").classList.add("hidden");
    $("confrontCtl").classList.add("hidden");   // não confrontar a partir de um run com erro
    $("verdictBadge").className = "verdict sell";
    $("verdictBadge").textContent = "ERRO";
    $("resultMeta").innerHTML = `<span>${escapeHtml(snap.error || "falha")}</span>`;
    $("bull").innerHTML = ""; $("bear").innerHTML = "";
    $("bullLead").textContent = ""; $("bearLead").textContent = "";
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

  // Estado do seletor de timeframe do ativo aberto. A escada vem do backend
  // (result.timeframes); runs antigas do histórico não a têm, então cai no
  // padrão por tipo de ativo. Toda análise começa exibindo o diário.
  _openDate = snap.date || "";
  _assetType = snap.asset_type || "";
  _tf = r.timeframe || "1d";
  _timeframes = Array.isArray(r.timeframes) && r.timeframes.length
    ? r.timeframes
    : (_assetType === "crypto" ? ["1w", "1d", "4h", "1h", "15m"] : ["1w", "1d"]);
  // TF em que o VEREDITO foi computado (carimbo do cabeçalho). Runs antigas não
  // têm o campo → cai no frame do gráfico. É diferente de _tf: _tf pode ser
  // trocado só pra olhar o gráfico, o carimbo fixa o frame do veredito real.
  _verdictTf = snap.verdict_timeframe || r.verdict_timeframe || r.timeframe || "1d";
  renderVerdictTf();
  renderDegraded(r.degraded);
  hideDegrade();

  renderHeadPrice(r.actionable);
  renderActionable(r.actionable);
  renderChartCard(r.price_chart, snap.ticker, r.actionable);
  renderTfSelector();

  renderThesis("bull", r.bull);
  renderThesis("bear", r.bear);

  const isCrypto = snap.asset_type === "crypto";
  let html = "";
  // Modo Erick (sob demanda): a leitura do método vem primeiro e ABERTA — é o que
  // o Samyr pediu em destaque (recuo à média, saída, peso do trade). Só aparece
  // quando o método Erick foi acionado; a análise Padrão não a tem.
  if (r.erick_report && r.erick_report.trim()) {
    html += `<details class="section erick" open><summary>🧭 Método Erick — recuo à média · saída · peso do trade</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.erick_report)}</div></div></details>`;
  }
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

// ---- comparação Padrão × Erick (meta-juiz) --------------------------------
// Bloco titulado do meta-juiz (concordância / divergência / significado). Renderiza
// markdown pra manter negrito e listas; nada é cortado — o texto flui inteiro.
function metaSection(title, md) {
  if (!md || !String(md).trim()) return "";
  return `<div class="mj-sec"><h3>${escapeHtml(title)}</h3><div class="md">${renderMarkdown(String(md))}</div></div>`;
}

// Uma coluna da comparação: título (método · timeframe), data, veredito, o GRÁFICO
// daquela análise (com seu TF/indicadores/marcações, interativo), plano operável,
// custo, flag de cache reusado, e atalho pra abrir a análise completa. ``slot`` é
// "A"/"B" (fixa o id do canvas pra o desenho depois do innerHTML).
function compareColumn(c, slot) {
  if (!c || !c.method) return "";
  const isErick = c.method === "erick";
  const title = (isErick ? "🧭 " : "") + (c.label || (isErick ? "Método Erick" : "Padrão"));
  const v = c.verdict || (c.status === "error" ? "error" : "");
  const plan = isErick
    ? (c.erick_report || c.trader_plan || c.final_decision || "")
    : (c.trader_plan || c.final_decision || "");
  const reused = c.reused
    ? `<span class="cmp-reused" title="reaproveitado do cache — não re-rodou">♻ cache</span>`
    : "";
  const dateStr = c.date ? `<span class="cmp-col-date">${escapeHtml(fmtDate(c.date))}</span>` : "";
  const deg = (Array.isArray(c.degraded) && c.degraded.length)
    ? `<div class="cmp-degraded">⚠️ Feito sem: ${c.degraded.map((d) => escapeHtml((d && (d.label || d.report_key)) || "fonte")).join(" · ")}</div>`
    : "";
  const err = c.status === "error"
    ? `<div class="cmp-err">Leitura indisponível: ${escapeHtml(c.error || "falha")}</div>`
    : "";
  const ch = c.price_chart;
  const hasChart = ch && Array.isArray(ch.candles) && ch.candles.length > 2;
  const chartCard = hasChart
    ? `<div class="cmp-chart-card">` +
        `<div class="chart-legend cmp-chart-legend">${chartLegendHtml(ch, c.actionable)}</div>` +
        `<div class="chart-wrap">` +
          `<span class="chart-zoom-hint">roda=zoom · shift+roda=vertical · arrasta=move · 2 cliques=reseta</span>` +
          `<canvas id="cmpChart${slot}" class="cmp-canvas"></canvas>` +
        `</div></div>`
    : "";
  return `<div class="cmp-col">` +
    `<div class="cmp-col-head"><span class="cmp-col-title">${escapeHtml(title)}${dateStr}</span>${reused}</div>` +
    `<div class="cmp-verdict-row"><span class="${verdictClass(v)}">${verdictHtml(v)}</span></div>` +
    deg + err +
    chartCard +
    `<div class="cmp-plan md">${renderMarkdown(plan)}</div>` +
    `<div class="cmp-col-foot"><span>${fmtCost(c.cost)} · ${c.elapsed || 0}s</span>` +
    (c.run_id ? `<button type="button" class="cmp-open" data-id="${escapeHtml(c.run_id)}">abrir análise completa →</button>` : "") +
    `</div></div>`;
}

// Desenha o gráfico de uma coluna no seu canvas (após o innerHTML existir). Cada
// canvas tem estado próprio → zoom/pan/reset independentes (h+v), como o principal.
function drawCompareChart(canvasId, col) {
  const cv = document.getElementById(canvasId);
  if (!cv) return;
  const chart = col && col.price_chart;
  if (!chart || !Array.isArray(chart.candles) || chart.candles.length <= 2) return;
  cv._chart = chart;
  cv._actionable = col.actionable || null;
  cv._view = null;
  cv._vview = null;
  drawPriceChart(cv, chart, cv._actionable);
  bindChartZoom(cv);
}

function renderCompare(snap) {
  clearInterval(pollTimer); pollTimer = null;
  $("runBtn").disabled = false;
  $("progressPanel").classList.add("hidden");
  $("resultPanel").classList.add("hidden");
  const panel = $("comparePanel");
  panel.classList.remove("hidden");

  const cmp = (snap.result || {}).compare || {};
  const meta = cmp.meta || {};
  const a = cmp.a || {}, b = cmp.b || {};
  const manual = !!cmp.manual;

  $("cmpAsset").textContent = snap.ticker || "—";
  const finished = snap.finished_at;
  // Auto "comparar" roda dois pipelines; o confronto manual reusa análises prontas.
  const costNote = manual ? "(análises já feitas)" : "(2 pipelines)";
  $("cmpMeta").innerHTML =
    `<span>Data <b>${escapeHtml(snap.date || "")}</b></span>` +
    `<span>Custo <b>${fmtCost(snap.cost)}</b> <span class="cmp-2p">${costNote}</span></span>` +
    `<span>Tempo <b>${snap.elapsed || 0}s</b></span>` +
    (finished ? `<span>Concluído <b>${fmtStamp(finished, true)}</b></span>` : "");

  const agr = meta.agreement || "";
  const agrCls = agr === "concordam" ? "agree" : (agr === "divergem" ? "diverge" : "partial");
  const agrLabel = agr === "concordam" ? "✅ Concordam" : (agr === "divergem" ? "⚠️ Divergem" : "◐ Parcial");
  $("metaJudge").innerHTML =
    `<div class="mj-head ${agrCls}">` +
      `<span class="mj-badge">${agrLabel}</span>` +
      `<span class="mj-headline">${escapeHtml(meta.headline || "")}</span>` +
    `</div>` +
    `<div class="mj-body">` +
      metaSection("Concordância", meta.concordancia) +
      metaSection("Divergência — o sinal", meta.divergencia) +
      metaSection("O que significa pra decisão", meta.significado) +
    `</div>`;

  $("cmpCols").innerHTML = compareColumn(a, "A") + compareColumn(b, "B");
  $("cmpCols").querySelectorAll("button.cmp-open").forEach((btn) =>
    btn.addEventListener("click", () => openRun(btn.dataset.id))
  );
  // desenha os dois gráficos (canvas já no DOM); cada um interativo e independente
  drawCompareChart("cmpChartA", a);
  drawCompareChart("cmpChartB", b);

  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  loadHistory();
}

// ---- confronto manual (Fase 3): esta análise × outra do mesmo ativo ----------
// Popula o seletor com as OUTRAS análises simples (não-comparação) do mesmo ticker.
function renderConfrontControl(snap) {
  const ctl = $("confrontCtl"), sel = $("confrontSelect");
  if (!ctl || !sel) return;
  const ticker = (snap.ticker || "").toUpperCase();
  const others = (_allRuns || []).filter((r) =>
    (r.ticker || "").toUpperCase() === ticker &&
    r.status === "done" &&
    r.run_id !== snap.run_id &&
    r.method !== "compare"
  );
  if (!others.length) { ctl.classList.add("hidden"); sel.innerHTML = ""; return; }
  sel.innerHTML = others.map((r) =>
    `<option value="${escapeHtml(r.run_id)}">${escapeHtml(confrontOptionLabel(r))}</option>`
  ).join("");
  ctl.classList.remove("hidden");
}
function confrontOptionLabel(r) {
  const m = r.method === "erick" ? "Erick" : (r.method === "padrao" ? "Padrão" : "");
  const tf = TF_LABEL[r.verdict_timeframe || "1d"] || (r.verdict_timeframe || "1d");
  const when = r.finished_at ? fmtStamp(r.finished_at) : (r.date || "");
  const v = VERDICT_PT[verdictKey(r.verdict || "")] || (r.verdict ? String(r.verdict).toUpperCase() : "");
  return [m, tf, v, when].filter(Boolean).join(" · ");
}
function bindConfront() {
  const btn = $("confrontBtn");
  if (!btn || btn._bound) return;
  btn._bound = true;
  btn.addEventListener("click", () => confront(_openRunId, $("confrontSelect").value));
}
async function confront(a, b) {
  if (!a || !b) return;
  $("formError").textContent = "";
  try {
    const res = await fetch("/api/compare", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ a, b }),
    });
    const snap = await res.json();
    if (!res.ok) { $("formError").textContent = snap.error || "falha ao confrontar"; return; }
    renderCompare(snap);
  } catch (e) { $("formError").textContent = "falha ao confrontar"; }
}

// ---- header price + setup strip -------------------------------------------
// O preço no momento da análise é a terceira âncora do cabeçalho (ticker ·
// veredito · preço), não uma linha perdida num card. Vem do plano acionável
// (último fechamento da série datada); ausente, o cabeçalho some com ele.
function renderHeadPrice(a) {
  const el = $("headPrice");
  if (!a || a.price == null) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.innerHTML = `<b>${fmtNum(a.price)}</b>` +
    (a.as_of ? `<span class="hp-when">em ${fmtDate(a.as_of)}</span>` : "");
  el.classList.remove("hidden");
}

// A faixa do setup carrega SÓ o que o gráfico não desenha: estado, horizonte e
// timeframe. As faixas de preço e o 1-2-3 agora vivem dentro do gráfico, então
// aqui não se repete um número sequer.
function renderActionable(a) {
  const el = $("actionable");
  if (!a || !a.setup_state) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const [emo, label] = SETUP_PT[a.setup_state] || ["⚪", a.setup_state];
  el.innerHTML =
    `<span class="act-setup ${escapeHtml(a.setup_state)}">${emo} ${escapeHtml(label)}</span>` +
    `<span class="act-fact"><span class="act-k">🕐 Horizonte</span> ${escapeHtml(a.horizon || "—")}</span>` +
    `<span class="act-fact"><span class="act-k">📐 Timeframe</span> ${escapeHtml(a.timeframe || "—")}</span>`;
  el.classList.remove("hidden");
}

// ---- thesis lead sentence -------------------------------------------------
// Cada tese abre com UMA frase tirada do PRÓPRIO texto — nunca inventada. Pega a
// primeira frase de prosa real (ignora títulos, listas, tabelas, código); sem
// prosa aproveitável, devolve o começo do texto. Texto vazio -> "" (o corpo já
// mostra "indisponível"). Retorna texto puro, exibido via textContent.
function stripInlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}
function clip(s, n) {
  return s.length > n ? s.slice(0, n - 1).replace(/\s+\S*$/, "") + "…" : s;
}
function leadSentence(md) {
  if (!md || !md.trim()) return "";
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let inFence = false;
  const prose = [];
  for (let raw of lines) {
    let line = raw.trim();
    if (/^```/.test(line)) { inFence = !inFence; continue; }
    if (inFence || !line) continue;
    if (/^#{1,6}\s/.test(line)) continue;             // título = rótulo, não prosa
    if (/^.{0,48}:\s*#{1,6}\s/.test(line)) continue;  // "Papel: ### Título" também é título
    if (/^([-*_]\s*){3,}$/.test(line)) continue;       // separador ---
    if (/^\|/.test(line)) continue;                    // linha de tabela
    line = line.replace(/^\s*([-*+]|\d+[.)]|>)\s+/, ""); // marcador de lista/citação
    line = stripInlineMd(line);
    line = line.replace(/^\s*(\d+[.)]|[-*+])\s+/, "");    // enumerador que sobrou após ênfase
    line = line.replace(/(^|\s)#{1,6}\s+/g, "$1").trim(); // hashes de título soltos na linha
    if (line.length < 3) continue;
    prose.push(line);
    if (prose.join(" ").length >= 90) break;           // já dá pra resumir com substância
  }
  let text = prose.join(" ").trim();
  if (!text) return "";
  // uma saudação de abertura ("Olá, pessoal!") não é resumo — descarta, mas só
  // quando sobra conteúdo real depois; nada é inventado, só se corta filler
  const noGreet = text.replace(/^(ol[áa]|oi|e a[íi]|bom dia|boa tarde|boa noite)[^.!?]*[.!?]\s+/i, "");
  if (noGreet.length >= 40) text = noGreet;
  const m = text.match(/^(.{40,}?[.!?])(\s|$)/);        // 1ª frase de substância
  return clip(m ? m[1] : text, 180);
}
function renderThesis(which, md) {
  $(which).innerHTML = renderMarkdown(md);
  const leadEl = $(which + "Lead");
  if (leadEl) leadEl.textContent = leadSentence(md);
  const box = $(which + "Box");
  if (box) box.open = false;   // corpo recolhido por padrão (só a frase-resumo)
}

// ---- candlestick chart (canvas, no external deps) -------------------------
// Moving-average colours, keyed by window. Chosen to stay legible on the dark
// panel and distinct from the green/red candles.
const MA_COLORS = { "20": "#f5b445", "50": "#6ea8fe", "200": "#b48ef5" };
const EMA_COLORS = { "8": "#4be3a0", "21": "#e3894b", "50": "#e34bd0" };
// 1-2-3 marker colour by direction — distinct so compra (fundo) and venda (topo)
// never read the same on the chart. Blue for compra, orange for venda; both stay
// clear of the green/red candle bodies.
const PAT_COLORS = { compra: "#6ea8fe", venda: "#ff9f43" };
function patColor(pat) {
  return (pat && PAT_COLORS[pat.direction]) || "#6ea8fe";
}
// Faixas do plano acionável desenhadas no gráfico: compra (verde), realização /
// alvo (dourado), recuo a aguardar (púrpura, só quando difere da compra).
const ZONE_COLORS = { buy: "#2ecc71", realize: "#f5b445", pullback: "#c084fc" };
// O estado de cada gráfico (dados + janela de zoom h/v + geometria) mora no próprio
// elemento <canvas> (canvas._chart/_actionable/_view/_vview/_yGeom/_autoY), pra que o
// gráfico principal e os dois da comparação sejam independentes.
let _tf = "1d";               // timeframe atualmente exibido no gráfico principal
let _timeframes = ["1d"];     // frames operáveis do ativo aberto (cripto = escada)
let _openDate = "";           // data da análise aberta (recomputa por timeframe)
let _assetType = "";          // tipo do ativo aberto (cripto habilita intradiário)
let _verdictTf = "1d";        // timeframe em que o veredito ABERTO foi computado (carimbo)

// paddings do gráfico, compartilhados entre o desenho e a interação de zoom/pan
// (o zoom vertical precisa converter y do cursor → preço com a MESMA geometria)
const PAD_L = 8, PAD_R = 58, PAD_T = 12, PAD_B = 22;

// Todos os frames do seletor, na ordem exibida. Um frame fora de `_timeframes`
// (intradiário em ação) é renderizado DESABILITADO — o backend nunca inventa
// candle pra ele; a UI só deixa claro que não é operável.
const ALL_TFS = [["1w", "Semanal"], ["1d", "Diário"], ["4h", "4h"], ["1h", "1h"], ["15m", "15m"]];
const TF_LABEL = { "1w": "Semanal", "1d": "Diário", "4h": "4h", "1h": "1h", "15m": "15m" };

// Legenda do gráfico (swatches das MMS/EMA + faixas do plano + 1-2-3). Extraída
// pra ser reusada pelos mini-gráficos da comparação.
function chartLegendHtml(chart, actionable) {
  const zones = planZones(actionable);
  const wins = (chart.ma_windows || [20, 50, 200]).map(String);
  const ewins = (chart.ema_windows || []).map(String);
  const pat = chart.markers && chart.markers.pattern_123;
  const legend = [];
  wins.forEach((w) => {
    if (MA_COLORS[w]) legend.push(`<span class="lg"><span class="sw" style="background:${MA_COLORS[w]}"></span>MMS${w}</span>`);
  });
  ewins.forEach((w) => {
    if (EMA_COLORS[w]) legend.push(`<span class="lg"><span class="sw" style="background:${EMA_COLORS[w]}"></span>EMA${w}</span>`);
  });
  zones.forEach((z) => legend.push(`<span class="lg"><span class="sw band" style="background:${z.color}"></span>${escapeHtml(z.tag)}</span>`));
  if (pat) {
    const [, dlabel] = PAT_DIR[pat.direction] || ["", ""];
    legend.push(`<span class="lg"><span class="sw dot" style="background:${patColor(pat)}"></span>1-2-3 ${escapeHtml(dlabel)}</span>`);
  }
  return legend.join("");
}

function renderChartCard(chart, ticker, actionable) {
  const card = $("chartCard");
  const cv = $("priceChart");
  const hasData = chart && Array.isArray(chart.candles) && chart.candles.length > 2;
  if (!hasData) { card.classList.add("hidden"); if (cv) cv._chart = null; return; }
  // Estado de zoom/pan vive no próprio canvas; novo gráfico recomeça na autoescala.
  cv._view = null;
  cv._vview = null;
  cv._chart = chart;
  cv._actionable = actionable || null;
  card.classList.remove("hidden");

  const active = chart.markers && chart.markers.active_region;
  const pat = chart.markers && chart.markers.pattern_123;
  card.classList.toggle("setup-active", !!active || (pat && pat.state === "acionado"));

  // As faixas do plano são as mesmas do plano acionável, agora desenhadas na
  // linha do preço em vez de repetidas em texto. buy/pullback coincidem no caso
  // "aguardar recuo" (mesma média) — desenha-se uma só (ver drawPriceChart).
  const zones = planZones(actionable);
  $("chartLegend").innerHTML = chartLegendHtml(chart, actionable);

  // note: pt-BR summary of what's marked
  const notes = [];
  if (active) {
    notes.push(`🎯 <b>Setup ativo agora</b>: preço na ${active.ma_label} (${Math.abs(active.distance_pct).toFixed(1)}% ${active.distance_pct >= 0 ? "acima" : "abaixo"}).`);
  }
  const nreg = (chart.markers && chart.markers.buy_regions || []).length;
  if (nreg) notes.push(`${nreg} região(ões) de compra na média marcada(s) no período.`);
  if (pat) {
    const [demo, dlabel] = PAT_DIR[pat.direction] || ["", ""];
    const verb = pat.direction === "venda" ? "perda de" : "rompimento de";
    notes.push(`${demo} Padrão 1-2-3 ${dlabel}: gatilho ${verb} ${fmtNum(pat.trigger)} — <b>${pat.state}</b>.`);
  }
  if (zones.length) notes.push("Faixas do plano rotuladas na linha do preço.");
  if (!notes.length) notes.push("Nenhum setup identificado na janela do gráfico.");
  $("chartNote").innerHTML = notes.join(" ");

  drawPriceChart(cv, chart, cv._actionable);
  bindChartZoom(cv);
}

// ---- timeframe selector ----------------------------------------------------
// Botões semanal · diário · 4h · 1h · 15m. Clicar recalcula região, 1-2-3 e faixas
// NAQUELE tempo gráfico (via /api/chart), redesenha o gráfico e marca o frame ativo.
// O semanal é reamostrado do diário, então vale pra ação e cripto; os intradiários
// fora de `_timeframes` (ação) ficam desabilitados — o backend não inventa candle.
function renderTfSelector() {
  const el = $("tfSelector");
  if (!el) return;
  const enabled = new Set(_timeframes || ["1d"]);
  el.innerHTML = ALL_TFS.map(([tf, label]) => {
    const on = enabled.has(tf);
    const active = tf === _tf;
    const cls = ["tf-btn", active ? "is-active" : "", on ? "" : "is-off"]
      .filter(Boolean).join(" ");
    const title = on
      ? `Recalcular no ${label}`
      : "Intradiário indisponível para ação (só cripto tem candle real de exchange)";
    return `<button type="button" class="${cls}" data-tf="${tf}" ${on ? "" : "disabled"} ` +
      `title="${escapeHtml(title)}">${escapeHtml(label)}</button>`;
  }).join("");
  bindTfSelector();
  renderReevalBtn();
}

// Banner de fonte degradada: nomeia a(s) fonte(s) que falhou(aram) MESMO após a
// nova tentativa automática, deixa explícito que a análise foi feita SEM ela(s), e
// oferece reavaliar incluindo-a(s) — o "informar + decidir" que o Samyr pediu, sem
// congelar o run server-side. Some quando nada degradou.
function renderDegraded(list) {
  const el = $("degradedBanner");
  if (!el) return;
  if (!Array.isArray(list) || !list.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const names = list.map((d) => escapeHtml((d && (d.label || d.report_key)) || "fonte")).join(" · ");
  const plural = list.length > 1;
  const reasons = list
    .filter((d) => d && d.reason)
    .map((d) => `<li><b>${escapeHtml(d.label || d.report_key || "fonte")}</b>: ${escapeHtml(d.reason)}</li>`)
    .join("");
  el.innerHTML =
    `<div class="dg-head">⚠️ Análise feita <b>SEM</b> ${plural ? "as fontes" : "a fonte"}: <b>${names}</b></div>` +
    `<div class="dg-sub">Tentei automaticamente mais uma vez antes de seguir. As leituras acima ` +
    `não incluem ${plural ? "essas fontes" : "essa fonte"} — trate como ausente, não como sinal.</div>` +
    (reasons ? `<ul class="dg-list">${reasons}</ul>` : "") +
    `<button type="button" class="dg-btn" id="reevalSourcesBtn">⟳ Reavaliar com ${plural ? "essas fontes" : "essa fonte"}</button>`;
  el.classList.remove("hidden");
  const btn = $("reevalSourcesBtn");
  // Reavaliar = rodar a análise inteira de novo (mesmo TF do veredito): a fonte
  // que caiu por transitório tende a voltar; o usuário decide pedir isso.
  if (btn) btn.addEventListener("click", () => reevaluate(_verdictTf));
}

// Carimbo do cabeçalho: "veredito no <frame>". Deixa explícito em qual timeframe
// o veredito aberto foi realmente computado (task 012) — some no run com erro.
function renderVerdictTf() {
  const el = $("verdictTf");
  if (!el) return;
  el.textContent = "veredito no " + (TF_LABEL[_verdictTf] || _verdictTf);
  el.classList.remove("hidden");
}

// Botão "reavaliar veredito neste TF": usa o frame ATUAL do gráfico (_tf). Quando
// já é o frame do veredito, fica desabilitado dizendo isso — não há o que refazer.
function renderReevalBtn() {
  const btn = $("reevalBtn");
  if (!btn) return;
  if (!_openTicker) { btn.classList.add("hidden"); return; }
  const label = TF_LABEL[_tf] || _tf;
  const same = _tf === _verdictTf;
  btn.textContent = same ? `✓ veredito já é no ${label}` : `⟳ Reavaliar veredito no ${label}`;
  btn.disabled = same;
  btn.classList.remove("hidden");
}

function bindReeval() {
  const btn = $("reevalBtn");
  if (!btn || btn._bound) return;
  btn._bound = true;
  btn.addEventListener("click", () => reevaluate(_tf));
}

// Reavaliação REAL: roda uma nova análise no timeframe escolhido (o analista de
// mercado lê a estrutura daquele frame); o veredito pode mudar. Mesma (ticker,data),
// só o TF muda. Reusa o método (Erick/Padrão) marcado no launcher.
function reevaluate(tf) {
  if (!_openTicker) return;
  const et = $("erickToggle");
  const method = et && et.checked ? "erick" : "padrao";
  $("resultPanel").classList.add("hidden");
  $("steps").innerHTML = "";
  renderProgress({
    status: "running", ticker: _openTicker, elapsed: 0, cost: null,
    progress: { phase: "Inicializando", label: `Reavaliando no ${TF_LABEL[tf] || tf}…`, percent: 2, plan: [], reached: [] },
  });
  fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker: _openTicker, date: _openDate || "", method, timeframe: tf }),
  })
    .then((r) => r.json())
    .then((data) => {
      if (data && data.run_id) { watchRun(data.run_id); loadHistory(); }
      else { $("formError").textContent = (data && data.error) || "falha ao reavaliar"; }
    })
    .catch(() => { $("formError").textContent = "falha ao reavaliar"; });
}

function bindTfSelector() {
  const el = $("tfSelector");
  if (!el || el._bound) return;
  el._bound = true;
  el.addEventListener("click", (e) => {
    const btn = e.target.closest("button.tf-btn");
    if (!btn || btn.disabled) return;
    switchTimeframe(btn.dataset.tf);
  });
}

function showDegrade(msg) {
  const el = $("chartDegrade");
  if (!el) return;
  el.textContent = "⚠️ " + msg;
  el.classList.remove("hidden");
}
function hideDegrade() {
  const el = $("chartDegrade");
  if (el) { el.textContent = ""; el.classList.add("hidden"); }
}

async function switchTimeframe(tf) {
  if (!_openTicker || tf === _tf) return;
  const prev = _tf;
  _tf = tf;
  renderTfSelector();                       // realce imediato no clicado
  hideDegrade();
  const note = $("chartNote");
  if (note) note.textContent = `Recalculando no ${TF_LABEL[tf] || tf}…`;
  try {
    const q = new URLSearchParams({ ticker: _openTicker, date: _openDate || "", tf });
    const res = await fetch("/api/chart?" + q.toString());
    const data = await res.json();
    if (!res.ok || data.error) {
      _tf = prev; renderTfSelector();
      if (note) note.textContent = data.error || "Falha ao recalcular timeframe.";
      return;
    }
    // O backend pode ter caído pro diário (fonte intradiária fora do ar); o
    // frame realmente exibido vem de data.timeframe, nunca uma barra inventada.
    _tf = data.timeframe || tf;
    if (Array.isArray(data.timeframes) && data.timeframes.length) _timeframes = data.timeframes;
    renderTfSelector();
    renderHeadPrice(data.actionable);
    renderActionable(data.actionable);
    renderChartCard(data.price_chart, _openTicker, data.actionable);
    if (data.degraded && data.notice) showDegrade(data.notice);
  } catch (err) {
    _tf = prev; renderTfSelector();
    if (note) note.textContent = "Falha ao recalcular timeframe.";
  }
}

// The plan's operable zones, ready to draw ON the chart (price on the band edge),
// de-duplicated: in "aguardar recuo" the buy zone and the pullback are the same
// rising average, so only one green band is drawn. A trigger-point pullback is a
// line the 1-2-3 already draws, so it is dropped here. Empty when no plan/levels.
function planZones(a) {
  if (!a) return [];
  const out = [];
  const buy = a.buy_zone;
  if (buy && buy.price != null) {
    const waiting = a.setup_state === "aguardar_pullback";
    out.push({ ...buy, color: ZONE_COLORS.buy, tag: waiting ? "compra (recuo à média)" : "compra" });
  }
  const rz = a.realize_zone;
  if (rz && rz.price != null) out.push({ ...rz, color: ZONE_COLORS.realize, tag: "realização (alvo)" });
  const pb = a.pullback_zone;
  const buyPrice = buy && buy.price;
  const isBand = pb && pb.low != null && pb.high != null;
  // só desenha o recuo separado quando é uma FAIXA distinta da compra (não o
  // gatilho-ponto do 1-2-3, que a própria marcação do padrão já traça)
  if (pb && pb.price != null && isBand && pb.price !== buyPrice) {
    out.push({ ...pb, color: ZONE_COLORS.pullback, tag: "recuo a aguardar" });
  }
  return out;
}

function fmtNum(v) {
  return (typeof v === "number" ? v : Number(v)).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// A small rounded chip with the price written ON a band/line edge, so the number
// lives on the chart where the level is — not repeated in a card below.
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
function drawEdgeLabel(ctx, ex, ey, text, bg, fg, align, clampH) {
  ctx.font = "bold 10.5px ui-monospace, Menlo, monospace";
  const padX = 5, h = 15;
  const w = ctx.measureText(text).width + padX * 2;
  const x0 = align === "right" ? ex - w : ex;
  let y0 = ey - h / 2;
  if (clampH != null) y0 = Math.max(1, Math.min(y0, clampH - h - 1));
  roundRect(ctx, x0, y0, w, h, 4);
  ctx.globalAlpha = 0.93; ctx.fillStyle = bg; ctx.fill(); ctx.globalAlpha = 1;
  ctx.fillStyle = fg; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(text, x0 + padX, y0 + h / 2 + 0.5);
}

function drawPriceChart(canvas, chart, a) {
  const candles = chart.candles;
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || canvas.parentElement.clientWidth || 640;
  const cssH = canvas.clientHeight || 380;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const padL = PAD_L, padR = PAD_R, padT = PAD_T, padB = PAD_B;
  const plotW = cssW - padL - padR, plotH = cssH - padT - padB;
  const n = candles.length;
  // Zoom/pan state lives ON the canvas element, so multiple charts (the main one
  // and the two in the comparison view) are independently interactive.
  const view = canvas._view || null;
  const vview = canvas._vview || null;
  let v0 = view ? Math.max(0, Math.min(view.v0, n - 8)) : 0;
  let v1 = view ? Math.max(v0 + 8, Math.min(view.v1, n)) : n;
  const vis = v1 - v0;
  // expõe a janela visível pra depuração/telemetria (e deixa o zoom observável)
  canvas.dataset.v0 = v0; canvas.dataset.v1 = v1; canvas.dataset.n = n;

  const zones = planZones(a);
  const price = a && a.price != null ? a.price : null;

  // price range across candles + MAs + pattern levels + plan zones + current
  // price — the plan bands are part of the picture now, so they must fit on screen
  let lo = Infinity, hi = -Infinity;
  const grow = (v) => { if (v != null && isFinite(Number(v))) { lo = Math.min(lo, v); hi = Math.max(hi, v); } };
  for (let i = v0; i < v1; i++) { grow(candles[i].l); grow(candles[i].h); }
  Object.values(chart.ma || {}).forEach((arr) => { for (let i = v0; i < v1; i++) grow(arr[i]); });
  Object.values(chart.ema || {}).forEach((arr) => { for (let i = v0; i < v1; i++) grow(arr[i]); });
  const pat = chart.markers && chart.markers.pattern_123;
  if (pat) [pat.p1.price, pat.p2.price, pat.p3.price, pat.trigger].forEach(grow);
  zones.forEach((z) => { grow(z.price); grow(z.low); grow(z.high); });
  grow(price);
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) { lo = 0; hi = 1; }
  const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;

  // Autoescala vertical (padrão) fica registrada em _autoY; a janela vertical
  // MANUAL (_vview) sobrepõe quando setada — é o zoom de preço que foca nas velas
  // quando uma média distante (ex.: MMS200) estica a escala e as espreme em cima.
  // Os rótulos e gridlines do eixo Y usam lo/hi, então respeitam a janela sozinhos.
  canvas._autoY = { lo, hi };
  if (vview && vview.hi > vview.lo) { lo = vview.lo; hi = vview.hi; }
  canvas._yGeom = { padT, plotH, lo, hi };
  canvas.dataset.plo = lo; canvas.dataset.phi = hi;

  const x = (i) => padL + (i - v0 + 0.5) * (plotW / vis);
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

  // timeframe stamp — o frame do padrão fica escrito NO gráfico (não só no card),
  // pra ninguém confundir um 1-2-3 de 15m com o do diário.
  const tfText = TF_LABEL[chart.timeframe] || chart.timeframe || "Diário";
  ctx.font = "bold 11px ui-monospace, Menlo, monospace";
  const tfW = ctx.measureText(tfText).width + 14;
  roundRect(ctx, padL + 2, padT + 2, tfW, 17, 4);
  ctx.globalAlpha = 0.85; ctx.fillStyle = "#1b2130"; ctx.fill(); ctx.globalAlpha = 1;
  ctx.strokeStyle = "rgba(255,255,255,0.12)"; ctx.lineWidth = 1; ctx.stroke();
  ctx.fillStyle = "#cdd6e4"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(tfText, padL + 9, padT + 2 + 8.5);

  // plan zones: translucent bands BEHIND the candles (edge labels drawn on top later)
  zones.forEach((z) => {
    const hasBand = z.low != null && z.high != null && z.high > z.low;
    if (hasBand) {
      const yTop = y(z.high), yBot = y(z.low);
      ctx.fillStyle = z.color + "1f";
      ctx.fillRect(padL, yTop, plotW, Math.max(2, yBot - yTop));
      ctx.strokeStyle = z.color + "55"; ctx.lineWidth = 1;
      ctx.strokeRect(padL + 0.5, yTop + 0.5, plotW - 1, Math.max(2, yBot - yTop));
    } else {
      const yy = y(z.price);
      ctx.strokeStyle = z.color + "aa"; ctx.setLineDash([5, 4]); ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
      ctx.setLineDash([]); ctx.lineWidth = 1;
    }
  });

  // date labels (x axis) — a handful, evenly spaced
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  const labels = 5;
  for (let t = 0; t <= labels; t++) {
    const i = Math.min(v1 - 1, v0 + Math.round((vis - 1) * (t / labels)));
    const d = candles[i].d; // YYYY-MM-DD -> DD/MM
    ctx.fillStyle = "#8b97ad";
    ctx.fillText(d.slice(8, 10) + "/" + d.slice(5, 7), x(i), padT + plotH + 5);
  }

  // candles
  const cw = Math.max(1, Math.min((plotW / vis) * 0.7, 14));
  candles.forEach((c, i) => {
    if (i < v0 || i >= v1) return;
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
  Object.entries(chart.ema || {}).forEach(([w, arr]) => {
    const color = EMA_COLORS[w]; if (!color) return;
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.setLineDash([4, 3]); ctx.beginPath();
    let st = false;
    arr.forEach((v, i) => {
      if (i < v0 || i >= v1) { st = false; return; }
      if (v == null) { st = false; return; }
      const px = x(i), py = y(v);
      if (!st) { ctx.moveTo(px, py); st = true; } else ctx.lineTo(px, py);
    });
    ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
  });
  Object.entries(chart.ma || {}).forEach(([w, arr]) => {
    const color = MA_COLORS[w]; if (!color) return;
    ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath();
    let started = false;
    arr.forEach((v, i) => {
      if (i < v0 || i >= v1) { started = false; return; }
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

  // 1-2-3 pattern: connecting line, labelled points (with price), trigger level
  // + trigger/state label. Colour and point kinds follow the direction — compra
  // (L-H-L, blue) vs venda (H-L-H, orange) — so the two never read the same.
  if (pat) {
    const col = patColor(pat);
    const kinds = pat.direction === "venda" ? ["H", "L", "H"] : ["L", "H", "L"];
    const pts = [["1", pat.p1], ["2", pat.p2], ["3", pat.p3]]
      .map(([lab, p], k) => ({ lab, kind: kinds[k], i: idx[p.date], price: p.price }))
      .filter((p) => p.i != null);
    if (pts.length) {
      ctx.strokeStyle = col; ctx.setLineDash([4, 3]); ctx.lineWidth = 1.5;
      ctx.beginPath();
      pts.forEach((p, k) => { const px = x(p.i), py = y(p.price); k ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
      ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
      // trigger horizontal line (translucent version of the direction colour)
      const ty = y(pat.trigger);
      ctx.strokeStyle = col + "80"; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(padL, ty); ctx.lineTo(padL + plotW, ty); ctx.stroke(); ctx.setLineDash([]);
      // point circles with number + price beside them
      pts.forEach((p) => {
        const px = x(p.i), cy = y(p.price), off = p.kind === "L" ? 14 : -14;
        ctx.font = "bold 12px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillStyle = "#0b0e14"; ctx.beginPath(); ctx.arc(px, cy + off, 8, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = col; ctx.stroke();
        ctx.fillStyle = col; ctx.fillText(p.lab, px, cy + off);
        ctx.font = "10px ui-monospace, Menlo, monospace"; ctx.fillStyle = "#8b97ad";
        ctx.fillText(fmtNum(p.price), px, cy + off + (p.kind === "L" ? 16 : -16));
      });
      // gatilho + estado, rotulado na borda DIREITA (a esquerda é das faixas do
      // plano — separar evita a colisão quando gatilho e compra ficam colados)
      const [semo] = PAT_STATE[pat.state] || ["⚪"];
      drawEdgeLabel(ctx, padL + plotW, ty, `1-2-3 gatilho ${fmtNum(pat.trigger)} ${semo}`, col, "#0b0e14", "right", cssH);
    }
  }

  // linha do preço atual, destacada, com o valor na borda direita ("agora")
  if (price != null) {
    const yp = y(price);
    ctx.strokeStyle = "rgba(230,234,242,0.5)"; ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, yp); ctx.lineTo(padL + plotW, yp); ctx.stroke();
    ctx.setLineDash([]);
    drawEdgeLabel(ctx, padL + plotW, yp, "agora " + fmtNum(price), "#e6eaf2", "#0b0e14", "right", cssH);
  }

  // faixas do plano: o preço escrito NA borda da banda (esquerda), por cima de
  // tudo pra ler claro. Faixa -> mín–máx; ponto -> o valor.
  zones.forEach((z) => {
    const hasBand = z.low != null && z.high != null && z.high > z.low;
    const yy = hasBand ? y(z.high) : y(z.price);
    const val = hasBand ? `${fmtNum(z.low)}–${fmtNum(z.high)}` : fmtNum(z.price);
    drawEdgeLabel(ctx, padL, yy, `${z.tag} ${val}`, z.color, "#0b0e14", "left", cssH);
  });
}

function bindChartZoom(canvas) {
  if (!canvas || canvas._zoomBound) return;
  canvas._zoomBound = true;
  // Todo o estado (janela h/v, geometria) mora no próprio canvas — assim cada
  // gráfico (o principal e os dois da comparação) tem zoom/pan independentes.
  const N = () => (canvas._chart && canvas._chart.candles ? canvas._chart.candles.length : 0);
  const redraw = () => drawPriceChart(canvas, canvas._chart, canvas._actionable);
  const cur = () => canvas._view || { v0: 0, v1: N() };

  // zoom VERTICAL (eixo de preço), ancorado no preço sob o cursor. Espelha o
  // horizontal: roda pra cima aproxima; zoom-out total solta pra autoescala.
  function zoomVerticalWheel(e) {
    const g = canvas._yGeom; if (!g) return;
    const rect = canvas.getBoundingClientRect();
    const plotH = Math.max(1, rect.height - PAD_T - PAD_B);
    const frac = Math.min(1, Math.max(0, (e.clientY - rect.top - PAD_T) / plotH)); // 0=topo,1=base
    const range = g.hi - g.lo;
    const anchor = g.hi - frac * range;                 // preço sob o cursor
    const factor = e.deltaY < 0 ? 0.82 : 1.22;          // roda pra cima = aproxima
    const autoRange = canvas._autoY ? (canvas._autoY.hi - canvas._autoY.lo) : range;
    const nr = range * factor;
    if (nr >= autoRange) { canvas._vview = null; redraw(); return; }  // zoom-out total = volta ao auto
    const nhi = anchor + frac * nr;
    canvas._vview = { lo: nhi - nr, hi: nhi };
    redraw();
  }

  canvas.addEventListener("wheel", (e) => {
    if (!N()) return;
    e.preventDefault();
    if (e.shiftKey) { zoomVerticalWheel(e); return; }   // shift+roda = eixo de preço
    const { v0, v1 } = cur(); const vis = v1 - v0;
    const rect = canvas.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left - 8) / (rect.width - 66)));
    const anchor = v0 + frac * vis;
    const factor = e.deltaY < 0 ? 0.82 : 1.22;
    let nv = Math.max(8, Math.min(N(), Math.round(vis * factor)));
    let nv0 = Math.max(0, Math.min(Math.round(anchor - frac * nv), N() - nv));
    canvas._view = (nv >= N()) ? null : { v0: nv0, v1: nv0 + nv };
    redraw();
  }, { passive: false });
  // Ponteiros ativos (mouse OU toques). 1 dedo = arrasta/move; 2 dedos = pinça = zoom.
  // No touch a roda não existe, então a pinça é o único jeito de aproximar — por isso
  // ela precisa funcionar já a partir da visão cheia (_view null).
  const pts = new Map();
  let drag = null;
  let pinch = null;
  let vdrag = null;   // pan VERTICAL: arrastar na faixa dos rótulos de preço (eixo direito)
  const fracX = (clientX) => {
    const rect = canvas.getBoundingClientRect();
    return Math.min(1, Math.max(0, (clientX - rect.left - 8) / (rect.width - 66)));
  };
  canvas.addEventListener("pointerdown", (e) => {
    if (!N()) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      const { v0, v1 } = cur();
      pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, v0, v1, frac: fracX((a.x + b.x) / 2) };
      drag = null;
    } else if (pts.size === 1) {
      const rect = canvas.getBoundingClientRect();
      const inAxis = (e.clientX - rect.left) >= (rect.width - PAD_R - 2); // faixa dos rótulos
      if (inAxis && canvas._yGeom) {
        // arrastar no eixo de preço = pan vertical (move a janela _vview)
        vdrag = { y: e.clientY, lo: canvas._yGeom.lo, hi: canvas._yGeom.hi };
        canvas.style.cursor = "ns-resize";
        drag = null;
      } else if (canvas._view) {
        drag = { x: e.clientX, v0: canvas._view.v0, v1: canvas._view.v1 };
        canvas.style.cursor = "grabbing";
      }
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pts.size >= 2) {
      const [a, b] = [...pts.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      const vis = pinch.v1 - pinch.v0;
      const factor = pinch.dist / dist;      // dedos afastam -> factor<1 -> aproxima (zoom in)
      let nv = Math.max(8, Math.min(N(), Math.round(vis * factor)));
      const anchor = pinch.v0 + pinch.frac * vis;
      let nv0 = Math.max(0, Math.min(Math.round(anchor - pinch.frac * nv), N() - nv));
      canvas._view = (nv >= N()) ? null : { v0: nv0, v1: nv0 + nv };
      redraw();
      return;
    }
    if (vdrag) {
      const rect = canvas.getBoundingClientRect();
      const plotH = Math.max(1, rect.height - PAD_T - PAD_B);
      const range = vdrag.hi - vdrag.lo;
      const shift = (e.clientY - vdrag.y) / plotH * range;   // arrastar pra baixo = janela sobe
      canvas._vview = { lo: vdrag.lo + shift, hi: vdrag.hi + shift };
      redraw();
      return;
    }
    if (drag) {
      const rect = canvas.getBoundingClientRect();
      const vis = drag.v1 - drag.v0;
      const dC = Math.round((e.clientX - drag.x) / (rect.width - 66) * vis);
      let nv0 = Math.max(0, Math.min(drag.v0 - dC, N() - vis));
      canvas._view = { v0: nv0, v1: nv0 + vis };
      redraw();
    }
  });
  const drop = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (pts.size === 0) { drag = null; vdrag = null; canvas.style.cursor = canvas._view ? "grab" : "default"; }
  };
  canvas.addEventListener("pointerup", drop);
  canvas.addEventListener("pointercancel", drop);
  // duplo-clique / duplo-toque reseta os DOIS eixos (horizontal + vertical)
  canvas.addEventListener("dblclick", () => { canvas._view = null; canvas._vview = null; redraw(); });
}

// redraw on resize so the canvas stays crisp and correctly scaled
let _resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(_resizeTimer);
  // Redesenha TODOS os gráficos vivos (principal + os dois da comparação), cada um
  // com seu próprio estado de zoom, pra ficarem nítidos e escalados após o resize.
  _resizeTimer = setTimeout(() => {
    document.querySelectorAll("canvas").forEach((cv) => {
      if (cv._chart) drawPriceChart(cv, cv._chart, cv._actionable);
    });
  }, 150);
});

// Histórico: painel fixo no desktop, faixa recolhível no mobile. matchMedia só
// dispara ao CRUZAR o limiar de 900px, então o jitter de viewport do celular
// (barra de endereço sumindo em scroll) não fecha um painel que o usuário abriu.
const _histMq = window.matchMedia("(max-width: 900px)");
function syncHistoryCollapse() {
  const p = document.getElementById("historyPanel");
  if (p) p.open = !_histMq.matches;
}
syncHistoryCollapse();
if (_histMq.addEventListener) _histMq.addEventListener("change", syncHistoryCollapse);
else if (_histMq.addListener) _histMq.addListener(syncHistoryCollapse);

// ---- polling & actions ----------------------------------------------------
// Acompanha UM run ao vivo: troca o alvo do polling (2s) sem matar o run anterior
// — ele segue rodando no servidor e continua visível na lista lateral. Só há um
// timer por vez; o guard em poll() descarta respostas tardias do run que saímos.
function watchRun(runId) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  _watchedRunId = runId;
  poll(runId);
  pollTimer = setInterval(() => poll(runId), 2000);
}

async function poll(runId) {
  try {
    const res = await fetch("/api/status/" + runId);
    if (!res.ok) throw new Error("status " + res.status);
    const snap = await res.json();
    if (runId !== _watchedRunId) return;   // já trocamos de run: ignora resposta tardia
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
  const et = $("erickToggle");
  const ct = $("compareToggle");
  const compare = !!(ct && ct.checked);
  const method = et && et.checked ? "erick" : "padrao";
  $("runBtn").disabled = true;
  $("resultPanel").classList.add("hidden");
  $("comparePanel").classList.add("hidden");
  $("steps").innerHTML = "";
  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, date, method, compare }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "falha ao iniciar");
    const boot = compare ? "Comparando Padrão × Erick…" : "Subindo o motor…";
    renderProgress({ status: "running", ticker, elapsed: 0, cost: null, progress: { phase: "Inicializando", label: boot, percent: 2, plan: [], reached: [] } });
    watchRun(data.run_id);
    loadHistory();   // o novo run aparece na lista como "em andamento" na hora
  } catch (e) {
    $("formError").textContent = e.message;
  } finally {
    // Reabilita já: um run em curso NÃO trava iniciar outro — os dois rodam em
    // paralelo no servidor. O botão só fica travado durante o POST (anti-duplo).
    $("runBtn").disabled = false;
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

    // Detecta runs que terminaram em SEGUNDO PLANO: estavam "em andamento" na
    // última atualização e agora não estão mais. Ganham um marcador "pronto" (ou
    // "erro") até o usuário abri-los. O run que está sendo assistido não conta —
    // o usuário já vê o resultado dele na tela.
    const runningIds = new Set(runs.filter((r) => r.status === "running").map((r) => r.run_id));
    _prevRunningIds.forEach((id) => {
      if (!runningIds.has(id) && id !== _watchedRunId) {
        const rec = runs.find((r) => r.run_id === id);
        if (rec) _finishedFlags.set(id, rec.status === "error" ? "error" : "done");
      }
    });
    _prevRunningIds = runningIds;

    if (!runs.length) { ul.innerHTML = '<li class="empty">Nenhuma análise ainda.</li>'; return; }
    // Duas famílias só: cripto e ações (metais entram em ações). O usuário
    // escolhe pela aba — Todos / Ações / Cripto —, sem cabeçalho empilhado.
    _allRuns = runs;
    // A lateral é a LISTA DE ATIVOS, não o log de execuções: um item por ticker,
    // com o veredito mais recente. O histórico daquele ativo (dias atrás) aparece
    // como calendário dentro da análise aberta.
    const item = (r, n) => {
      const running = r.status === "running";
      const v = (r.verdict || r.status || "").toString();
      const badge = n > 1 ? `<span class="h-count" title="${n} análises">${n}</span>` : "";
      // marcador de término em 2º plano (só em run já concluído, some ao abrir)
      const flag = !running && _finishedFlags.get(r.run_id);
      const flagHtml = flag
        ? `<span class="h-flag ${flag}">${flag === "error" ? "⚠ erro" : "✓ pronto"}</span>`
        : "";
      let vHtml, vClass, meta;
      if (running) {
        const p = r.progress || {};
        vHtml = `<span class="run-dot"></span>${p.percent || 0}%`;
        vClass = "running";
        meta = `${escapeHtml(p.phase || "processando")} · ${Math.round(r.elapsed || 0)}s`;
      } else {
        vHtml = verdictHtml(v);
        vClass = verdictClass(v).replace("verdict", "").trim();
        const when = r.finished_at ? fmtStamp(r.finished_at) : escapeHtml(r.date || "");
        meta = `${when} · ${fmtCost({ usd: r.cost_usd || 0 })} · ${r.elapsed || 0}s`;
      }
      return `<li data-id="${escapeHtml(r.run_id)}" class="${running ? "is-running" : ""}">` +
        `<span class="h-ticker">${escapeHtml(r.ticker || "?")}${badge}${flagHtml}</span>` +
        `<span class="h-verdict ${vClass}" title="${escapeHtml(running ? "em andamento" : v)}">${vHtml}</span>` +
        `<span class="h-meta">${meta}</span>` +
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
    _finishedFlags.delete(runId);          // abriu: some o marcador "pronto/erro"
    const res = await fetch("/api/run/" + runId);
    const snap = await res.json();
    if (!res.ok) return;
    if (snap.status === "running") {
      // reabre um run EM ANDAMENTO: volta a acompanhar ao vivo (re-liga o polling
      // daquele run_id). O run nunca parou — só a visão tinha saído dele.
      _openTicker = snap.ticker || "";
      $("resultPanel").classList.add("hidden");
      renderProgress(snap);
      watchRun(runId);
    } else {
      renderResult(snap);
    }
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
  bindReeval();
  bindConfront();
  loadHistory().then(openLatestRun);
  startHistoryAutoRefresh();
}

// A lista de fundo se atualiza devagar (5s), independente do run assistido: é o
// que faz um run em andamento APARECER na lateral, o progresso dele avançar ali, e
// o marcador "pronto" surgir quando ele termina sozinho enquanto você olha outro.
function startHistoryAutoRefresh() {
  if (_historyTimer) clearInterval(_historyTimer);
  _historyTimer = setInterval(loadHistory, 5000);
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
