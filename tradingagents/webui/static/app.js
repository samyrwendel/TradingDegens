"use strict";

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let TZ_LABEL = "GMT-4 (Manaus)";

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
  clearInterval(pollTimer); pollTimer = null;
  $("runBtn").disabled = false;
  $("progressPanel").classList.add("hidden");
  const panel = $("resultPanel");
  panel.classList.remove("hidden");

  if (snap.status === "error") {
    $("verdictBadge").className = "verdict sell";
    $("verdictBadge").textContent = "ERRO";
    $("resultMeta").innerHTML = `<span>${escapeHtml(snap.error || "falha")}</span>`;
    $("bull").innerHTML = ""; $("bear").innerHTML = "";
    $("sections").innerHTML = section("Trace", "```\n" + ((snap.result && snap.result.trace) || "") + "\n```");
    return;
  }

  const r = snap.result || {};
  $("verdictBadge").className = verdictClass(r.verdict);
  $("verdictBadge").textContent = (r.verdict || "—").toUpperCase();
  const finished = snap.finished_at || (snap.result && snap.result.finished_at);
  $("resultMeta").innerHTML =
    `<span>Ativo <b>${escapeHtml(snap.ticker)}</b></span>` +
    `<span>Data da análise <b>${escapeHtml(snap.date || "")}</b></span>` +
    `<span>Tipo <b>${escapeHtml(snap.asset_type || "")}</b></span>` +
    `<span>Custo <b>${fmtCost(snap.cost)}</b></span>` +
    `<span>Tempo <b>${snap.elapsed || 0}s</b></span>` +
    (finished ? `<span>Concluído <b>${fmtStamp(finished, true)}</b></span>` : "");

  $("bull").innerHTML = renderMarkdown(r.bull);
  $("bear").innerHTML = renderMarkdown(r.bear);

  const isCrypto = snap.asset_type === "crypto";
  let html = "";
  // For crypto, the deterministic derivatives feed goes first and open — it is
  // the data yfinance can't see and the source is always named here.
  if (isCrypto && r.derivatives_report && r.derivatives_report.trim()) {
    html += `<details class="section" open><summary>🪙 Derivativos — funding · open interest · liquidações (fonte nomeada)</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.derivatives_report)}</div></div></details>`;
  }
  html += section("⚖️ Juiz do Debate (Gestor de Pesquisa)", r.research_manager || r.investment_plan);
  html += section("📊 Mercado — preço e multi-timeframe", r.market_report);
  html += section("📰 Notícias — macro e prediction markets", r.news_report);
  html += section("💬 Sentimento", r.sentiment_report);
  if (!isCrypto) html += section("📑 Fundamentos", r.fundamentals_report);
  html += section("🎯 Plano do Trader", r.trader_plan);
  html += section("🛡️ Decisão de Risco (veredito final na íntegra)", r.risk_decision || r.final_trade_decision);
  $("sections").innerHTML = html;

  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  loadHistory();
}

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

async function loadHistory() {
  try {
    const res = await fetch("/api/history");
    const data = await res.json();
    const ul = $("history");
    const runs = data.runs || [];
    if (!runs.length) { ul.innerHTML = '<li class="empty">Nenhuma análise ainda.</li>'; return; }
    ul.innerHTML = runs.map((r) => {
      const v = (r.verdict || r.status || "").toString();
      const when = r.finished_at ? fmtStamp(r.finished_at) : escapeHtml(r.date || "");
      return `<li data-id="${escapeHtml(r.run_id)}">` +
        `<span class="h-ticker">${escapeHtml(r.ticker || "?")}</span>` +
        `<span class="h-verdict ${verdictClass(v).replace("verdict", "").trim()}">${escapeHtml(v.toUpperCase())}</span>` +
        `<span class="h-meta">${when} · ${fmtCost({ usd: r.cost_usd || 0 })} · ${r.elapsed || 0}s</span>` +
        `</li>`;
    }).join("");
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
    if (cfg.today) $("date").value = cfg.today;
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
    $("assetHint").textContent = /-(USD|USDT)$|^BTC|^ETH/.test(t) ? "Detectado: cripto — inclui funding, OI e liquidações." : "";
  });
  $("netNote").textContent = "acesse por " + location.host;
  loadHistory();
}

document.addEventListener("DOMContentLoaded", init);
