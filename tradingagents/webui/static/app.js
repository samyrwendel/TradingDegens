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
let _openMethod = "padrao";          // método da análise aberta (Erick EMA 8/21 / Padrão MMS) — troca de TF mantém a estrutura do método
let _openView = "padrao";            // o que a barra de reanálise destaca: "padrao" | "erick" | "compare" (compare = view de comparação aberta). Clicar o destaque = "Atualizar" (reanalisa hoje preservando o método).
let _prevRunningIds = new Set();     // ids que apareciam "running" na última lista
const _finishedFlags = new Map();    // run_id -> "done" | "error" (terminou em 2º plano)
let _historyTimer = null;            // atualização lenta da lista (marcadores vivos)

// ---- BYOK: config de LLM do usuário (traga sua chave) -----------------------
// A chave vive SÓ no navegador (localStorage) e viaja por header X-LLM-Key —
// nunca em querystring, nunca gravada no servidor. Sem chave, o servidor usa a
// env dele (fallback). O restante (provider/modelo/base_url) vai no corpo do POST.
const _LLM_CFG_KEY = "td_llm_cfg";
let _llmCfg = {};              // { provider, apiKey, deepModel, quickModel, baseUrl }
let _llmMeta = null;           // catálogo de providers vindo de /api/config
// Login do dono: destrava a chave do SERVIDOR (público usa a própria). Estado vem
// do servidor (cookie de sessão HttpOnly) — nunca de fachada no cliente.
let _isOwner = false;
let _ownerLoginEnabled = false;

function loadLlmCfg() {
  try { _llmCfg = JSON.parse(localStorage.getItem(_LLM_CFG_KEY) || "{}") || {}; }
  catch (e) { _llmCfg = {}; }
  return _llmCfg;
}
function saveLlmCfg(cfg) {
  _llmCfg = cfg || {};
  try { localStorage.setItem(_LLM_CFG_KEY, JSON.stringify(_llmCfg)); } catch (e) { /* quota */ }
}

// ---- Run ativo persistido: reengate após refresh / voltar de 2º plano --------
// A análise roda numa thread no SERVIDOR e sobrevive a refresh, troca de ativo e
// app em segundo plano. Só a VISÃO se perdia: o pollTimer morria (clearInterval em
// vários pontos, ou o throttle de aba de fundo no mobile) e ninguém reengatava —
// o front "esquecia" a análise viva. Guardamos {run_id, ticker} enquanto um run
// está sendo acompanhado ao vivo; ao carregar a página e ao voltar o app pra
// frente, se ele ainda está `running` no servidor, reengatamos. Some no término.
const _ACTIVE_RUN_KEY = "td_active_run";
function saveActiveRun(runId, ticker) {
  if (!runId) return;
  try {
    localStorage.setItem(_ACTIVE_RUN_KEY, JSON.stringify({ run_id: runId, ticker: ticker || "" }));
  } catch (e) { /* quota / modo privado: perde só o reengate, não quebra */ }
}
function clearActiveRun() {
  try { localStorage.removeItem(_ACTIVE_RUN_KEY); } catch (e) { /* ignore */ }
}
function loadActiveRun() {
  try { return JSON.parse(localStorage.getItem(_ACTIVE_RUN_KEY) || "null"); }
  catch (e) { return null; }
}

// Header + campos de corpo pra uma requisição que roda LLM (analyze/compare/ask).
// A chave SÓ no header; provider/modelo/base_url só quando o usuário definiu.
function llmRequestParts() {
  const c = _llmCfg || {};
  const headers = { "Content-Type": "application/json" };
  if (c.apiKey) headers["X-LLM-Key"] = c.apiKey;
  const body = {};
  if (c.provider) body.llm_provider = c.provider;
  if (c.deepModel) body.deep_think_llm = c.deepModel;
  if (c.quickModel) body.quick_think_llm = c.quickModel;
  if (c.baseUrl) body.backend_url = c.baseUrl;
  return { headers, body };
}

// POST que carrega a config BYOK. Drop-in pros fetch de /api/analyze|compare|ask.
// credentials:'same-origin' garante o cookie de sessão do dono na requisição.
function apiPost(url, payload) {
  const { headers, body } = llmRequestParts();
  return fetch(url, {
    method: "POST",
    headers,
    credentials: "same-origin",
    body: JSON.stringify({ ...(payload || {}), ...body }),
  });
}

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

// Ao abrir um resultado/comparação, rola pra faixa de reanálise (quando visível)
// em vez do painel — assim o controle de método/TF fica no topo da visão, logo à
// mão, e não some scrollado acima da borda. Sem a faixa, rola pro próprio painel.
function scrollToOpen(panel) {
  const bar = $("reanalyzeBar");
  const target = bar && !bar.classList.contains("hidden") ? bar : panel;
  if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderProgress(snap) {
  $("progressPanel").classList.remove("hidden");
  // a faixa de reanálise só faz sentido com um resultado/comparação na tela;
  // enquanto uma análise roda ela some (reaparece quando o resultado renderiza).
  const reBar = $("reanalyzeBar");
  if (reBar) reBar.classList.add("hidden");
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
  const cmpStepsEl = $("compareSteps");

  // Progresso de CONFRONTO: trilha de 3 etapas (Padrão → Erick → Comparação) com
  // o estado de cada — em vez dos chips de analista da análise única.
  // Raciocínio ao vivo: revela o texto dos agentes conforme terminam (task 008).
  renderThinking(snap.thinking);

  if (p.compare_steps && p.compare_steps.length) {
    steps.classList.add("hidden");
    cmpStepsEl.classList.remove("hidden");
    cmpStepsEl.innerHTML = p.compare_steps.map((s, i) => {
      const st = s.state || "pending";
      const icon = { pending: "○", running: "⏳", done: "✅", reused: "♻" }[st] || "○";
      const stateTxt = { pending: "aguardando", running: "rodando…", done: "concluída", reused: "reusada do cache" }[st] || "";
      return `<li class="cmp-step is-${st}">` +
        `<span class="cmp-step-n">${i + 1}</span>` +
        `<span class="cmp-step-icon">${icon}</span>` +
        `<span class="cmp-step-body"><span class="cmp-step-label">${escapeHtml(s.label)}</span>` +
        `<span class="cmp-step-state">${stateTxt}</span></span></li>`;
    }).join("");
    return;
  }

  // Análise única: chips dos analistas (plano + alcançados).
  cmpStepsEl.classList.add("hidden");
  steps.classList.remove("hidden");
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

// Raciocínio AO VIVO (task 008): renderiza os pareceres dos agentes conforme
// CHEGAM no snapshot (mercado → sentimento → … → debate → juiz → risco). Faz
// UPSERT por card (data-tk) e só re-renderiza o corpo quando o texto CRESCE — nada
// de recriar o DOM inteiro a cada 2s (senão "dança"). Cada card é colapsável; o
// debate ganha destaque. O container tem altura máxima e rola por dentro (mobile).
// Zera o painel de raciocínio (troca de run / novo run): sem isso os cards de uma
// análise anterior ficariam misturados com a nova.
function resetThinking() {
  const box = $("thinkingLive");
  if (box) { box.innerHTML = ""; box.classList.add("hidden"); }
}

function renderThinking(items) {
  const box = $("thinkingLive");
  if (!box) return;
  if (!Array.isArray(items) || !items.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  items.forEach((it) => {
    let card = box.querySelector(`[data-tk="${cssEsc(it.id)}"]`);
    if (!card) {
      card = document.createElement("details");
      card.className = "tk-card" + (it.debate ? " tk-debate" : "");
      card.dataset.tk = it.id;
      card.dataset.order = String(it.order);
      card.open = true;   // abre conforme chega — o Samyr quer VER o pensamento
      const sum = document.createElement("summary");
      sum.className = "tk-sum";
      sum.textContent = it.label;
      const body = document.createElement("div");
      body.className = "tk-body md";
      card.appendChild(sum);
      card.appendChild(body);
      // insere na posição do pipeline (mantém a ordem mesmo se chegar fora de ordem)
      const after = [...box.children].find(
        (c) => Number(c.dataset.order) > it.order
      );
      box.insertBefore(card, after || null);
    }
    const body = card.querySelector(".tk-body");
    // re-renderiza só quando o texto mudou de tamanho (streaming/parcial→final)
    if (body && body.dataset.len !== String(it.len)) {
      body.innerHTML = renderMarkdown(it.text || "");
      body.dataset.len = String(it.len);
    }
  });
}

// Escapa um id pra usar em querySelector([data-tk="..."]) sem quebrar com caracteres
// especiais (os ids são nomes de nó com espaços). Usa CSS.escape quando existe.
function cssEsc(s) {
  s = String(s);
  return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/["\\\]]/g, "\\$&");
}

// Selo de eixo (item 8): {eixo · horizonte} — mostra que o módulo opera numa
// camada, não numa decisão concorrente. Vazio quando o run não trouxe axes.
function axisTag(axis) {
  if (!axis || !axis.eixo) return "";
  const h = axis.horizonte ? ` · ${escapeHtml(axis.horizonte)}` : "";
  return ` <span class="axis-tag">eixo ${escapeHtml(axis.eixo)}${h}</span>`;
}

// Checagem de consistência (item 7): o resultado do checker de contradições pré-
// publicação. Aberto e no topo quando há achados (é um portão de QA); um selo verde
// discreto quando limpo. Vazio quando o run não trouxe o campo.
const _SEV_ICON = { alta: "🔴", "média": "🟡", baixa: "🟢" };
function contradictionsHtml(findings) {
  if (findings === undefined || findings === null) return "";
  if (!Array.isArray(findings) || findings.length === 0) {
    return `<div class="consistency-ok">✅ Checagem de consistência: sem inconsistências ` +
      `(decisão única · gatilho 1-2-3 coerente · preço único · agregados batem).</div>`;
  }
  const items = findings.map((f) => {
    const icon = _SEV_ICON[f && f.severity] || "•";
    return `<li>${icon} <b>${escapeHtml((f && f.code) || "")}</b>: ` +
      `${escapeHtml((f && f.message) || "")}</li>`;
  }).join("");
  return `<details class="section consistency-warn" open>` +
    `<summary>⚠️ Checagem de consistência — ${findings.length} inconsistência(s) a revisar</summary>` +
    `<div class="section-body"><ul class="consistency-list">${items}</ul></div></details>`;
}

function section(title, mdText, axis) {
  if (!mdText || !mdText.trim()) return "";
  return `<details class="section"><summary>${escapeHtml(title)}${axisTag(axis)}</summary>` +
    `<div class="section-body"><div class="md">${renderMarkdown(mdText)}</div></div></details>`;
}

// Rodapé de auditoria (item 10): run_id + timestamp único da coleta + versão do
// pipeline + modelo por camada de agente. Sem isto não dá pra atribuir regressão
// entre runs. Só renderiza quando o run trouxe o bloco audit.
function auditFooterHtml(audit, asOfPrice) {
  if (!audit || !audit.run_id) return "";
  const m = audit.models || {};
  const models = [
    m.provider ? `provedor ${escapeHtml(m.provider)}` : "",
    m.deep_think ? `deep ${escapeHtml(m.deep_think)}` : "",
    m.quick_think ? `quick ${escapeHtml(m.quick_think)}` : "",
  ].filter(Boolean).join(" · ");
  const price = (asOfPrice !== null && asOfPrice !== undefined)
    ? ` · preço de referência ${escapeHtml(String(asOfPrice))}` : "";
  return `<div class="audit-footer">` +
    `run ${escapeHtml(audit.run_id)} · coleta ${escapeHtml(audit.collected_at || "—")} · ` +
    `pipeline v${escapeHtml(audit.pipeline_version || "—")}${price}` +
    (models ? ` · modelos: ${models}` : "") +
    `</div>`;
}

// Banner de erro HUMANO: mensagem acionável em pt-BR (nunca stack, nunca chave).
// Erros de chave/crédito (no_credit/invalid_key) ganham um botão que abre o painel
// de Configurações; os demais só a mensagem + dica de tentar de novo.
const _CFG_ERROR_CODES = new Set(["no_credit", "invalid_key"]);
function errorCardHtml(message, code) {
  const msg = message || "Falha ao rodar a análise.";
  const wantsConfig = _CFG_ERROR_CODES.has(code);
  const action = wantsConfig
    ? `<button type="button" class="err-action" data-act="open-config">⚙️ Abrir Configurações</button>`
    : `<span class="err-hint">Você pode tentar de novo pelos botões de método/timeframe acima.</span>`;
  return `<div class="error-card ${escapeHtml(code || "error")}">` +
    `<div class="err-title">⚠️ Não deu pra concluir</div>` +
    `<div class="err-msg">${escapeHtml(msg)}</div>` +
    `<div class="err-foot">${action}</div></div>`;
}
function bindErrorCard(container) {
  const btn = container && container.querySelector('[data-act="open-config"]');
  if (btn) btn.addEventListener("click", () => {
    $("configPanel").classList.remove("hidden");
    scrollToOpen($("configPanel"));
  });
}

function renderResult(snap) {
  // este run passa a ser o "aberto" na tela: enquanto for ele, um término dele
  // NÃO vira aviso "pronto" (o usuário já está vendo o resultado).
  clearActiveRun();   // resultado na tela = nada de run vivo a reengatar
  _watchedRunId = snap.run_id || _watchedRunId;
  $("comparePanel").classList.add("hidden");
  // Run de comparação (Padrão × Erick): view própria, lado a lado.
  if ((snap.result || {}).compare) { renderCompare(snap); return; }
  const nameEl = document.getElementById("assetName");
  if (nameEl) {
    // Cabeçalho no formato TICKER ( Nome ) — o nome resolve async (cacheado);
    // enquanto não resolve, mostra só o ticker (nunca inventa nome).
    nameEl.innerHTML = snap.ticker ? tickerLabelHtml(snap.ticker) : "—";
    if (snap.ticker) {
      ensureNames([snap.ticker]).then((ch) => {
        if (ch && _openTicker === snap.ticker) nameEl.innerHTML = tickerLabelHtml(snap.ticker);
      });
    }
  }
  _openTicker = snap.ticker || "";
  _openRunId = snap.run_id || "";
  // Exportar PDF só faz sentido com conteúdo real: escondido no estado de erro (abaixo).
  const ex = document.getElementById("exportPdfBtn");
  if (ex) ex.classList.remove("hidden");
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
    $("exportPdfBtn").classList.add("hidden");  // nada de análise pra exportar num run com erro
    $("confrontCtl").classList.add("hidden");   // não confrontar a partir de um run com erro
    // Reanálise segue disponível: uma falha (fonte fora do ar, transitório) é
    // justamente quando o usuário quer rerodar escolhendo método/TF, sem redigitar.
    // Método aberto: preserva o que o run errado carregava (history traz r.method);
    // sem isso, sem destaque (não inventa método num run que falhou).
    _openMethod = snap.method === "erick" ? "erick" : (snap.method === "padrao" ? "padrao" : _openMethod);
    _openView = snap.method === "compare" ? "compare"
      : (snap.method === "erick" ? "erick" : (snap.method === "padrao" ? "padrao" : ""));
    _openDate = snap.date || "";
    _assetType = snap.asset_type || "";
    // Escada completa: intradiário vale pra ação e cripto (fonte real keyless dos
  // dois; frame sem candle degrada honesto sob demanda). Só o fallback — a fonte
  // da verdade é result.timeframes do backend.
  _timeframes = ["1w", "1d", "4h", "1h", "15m"];
    _verdictTf = snap.verdict_timeframe || "1d";
    _reTf = _timeframes.includes(_verdictTf) ? _verdictTf : "1d";
    renderReanalyzeBar();
    $("verdictBadge").className = "verdict sell";
    $("verdictBadge").textContent = "ERRO";
    $("resultMeta").innerHTML = "";
    $("bull").innerHTML = ""; $("bear").innerHTML = "";
    $("bullLead").textContent = ""; $("bearLead").textContent = "";
    // O banner ocupa o lugar do RESULTADO: esconde as teses vazias (Alta/Baixa) —
    // sem dado, mostrá-las é ruído. A faixa de reanálise segue visível acima.
    const railTheses = document.querySelector(".rail-theses");
    if (railTheses) railTheses.classList.add("hidden");
    // Banner de erro HUMANO (sem stack, sem chave): a mensagem acionável do backend
    // + botão pra abrir ⚙️ Configurações quando é problema de chave/crédito.
    $("sections").innerHTML = errorCardHtml(snap.error, snap.error_code);
    bindErrorCard($("sections"));
    mountAskBox($("askSingle"), "");  // run com erro não tem dado pra ancorar pergunta
    return;
  }

  const r = snap.result || {};
  // reexibe as teses (podem ter sido escondidas por um render de erro anterior).
  const railThesesOk = document.querySelector(".rail-theses");
  if (railThesesOk) railThesesOk.classList.remove("hidden");
  // método da análise aberta: a estrutura (recuo/1-2-3) é EMA 8/21 no Erick, MMS no
  // Padrão. Trocar de TF precisa recalcular na mesma família — daí guardar o método.
  _openMethod = (r.erick_report && r.erick_report.trim()) ? "erick" : "padrao";
  _openView = _openMethod;   // a barra destaca o método aberto (Padrão/Erick)
  $("verdictBadge").className = verdictClass(r.verdict);
  $("verdictBadge").innerHTML = verdictHtml(r.verdict);
  const finished = snap.finished_at || (snap.result && snap.result.finished_at);
  $("resultMeta").innerHTML =

    `<span>Data da análise <b>${escapeHtml(snap.date || "")}</b></span>` +
    `<span>Tipo <b>${escapeHtml(assetPt(snap.asset_type))}</b></span>` +
    `<span>Custo <b>${fmtCost(snap.cost)}</b></span>` +
    `<span>Tempo <b>${snap.elapsed || 0}s</b></span>` +
    (finished ? `<span>Concluído <b>${fmtStamp(finished, true)}</b></span>` : "");

  // Estado do seletor de timeframe do ativo aberto. Operabilidade é propriedade do
  // ATIVO HOJE, não um congelado da run: ação e cripto têm a escada intradiária
  // inteira agora, então uma run ANTIGA (salva quando ação só tinha 1w/1d) também
  // ganha os botões intradiários ao reabrir. O backend (/api/chart) é o árbitro
  // real e degrada honesto por símbolo/data. Toda análise começa exibindo o diário.
  _openDate = snap.date || "";
  _assetType = snap.asset_type || "";
  _tf = r.timeframe || "1d";
  _timeframes = ["1w", "1d", "4h", "1h", "15m"];
  // TF em que o VEREDITO foi computado (carimbo do cabeçalho). Runs antigas não
  // têm o campo → cai no frame do gráfico. É diferente de _tf: _tf pode ser
  // trocado só pra olhar o gráfico, o carimbo fixa o frame do veredito real.
  _verdictTf = snap.verdict_timeframe || r.verdict_timeframe || r.timeframe || "1d";
  renderVerdictTf();
  _reTf = _verdictTf;                 // a reanálise começa no frame do veredito aberto
  renderReanalyzeBar();
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
  const axes = r.axes || {};
  // Portão de QA (item 7): a checagem de consistência vai no TOPO das seções.
  html += contradictionsHtml(r.contradictions);
  if (r.erick_report && r.erick_report.trim()) {
    html += `<details class="section erick" open><summary>🧭 Método Erick — recuo à média · saída · peso do trade${axisTag(axes.erick)}</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.erick_report)}</div></div></details>`;
  }
  // For crypto, the deterministic derivatives feed goes first and open — it is
  // the data yfinance can't see and the source is always named here.
  if (isCrypto && r.derivatives_report && r.derivatives_report.trim()) {
    html += `<details class="section" open><summary>🪙 Derivativos — taxa de financiamento <span class="orig">(funding)</span> · contratos em aberto <span class="orig">(open interest)</span> · liquidações <span class="orig">(liquidations)</span> (fonte nomeada)</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.derivatives_report)}</div></div></details>`;
  }
  html += section("⚖️ Juiz do Debate (Gestor de Pesquisa) — leitura", r.research_manager || r.investment_plan, axes.juiz);
  html += section("📊 Mercado — preço e múltiplos tempos gráficos", r.market_report, axes.tecnico);
  html += section("📰 Notícias — macro e mercados de previsão", r.news_report);
  html += section("💬 Sentimento", r.sentiment_report);
  if (!isCrypto) html += section("📑 Fundamentos", r.fundamentals_report);
  html += section("🎯 Plano do Trader (leitura — insumo, não é o veredito)", r.trader_plan, axes.trader);
  html += section("🛡️ Decisão de Risco (veredito final na íntegra — a única decisão)", r.risk_decision || r.final_trade_decision, axes.veredito);
  html += auditFooterHtml(r.audit, r.as_of_price);
  $("sections").innerHTML = html;

  mountAskBox($("askSingle"), snap.run_id);

  scrollToOpen(panel);
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
  const openBtn = c.run_id
    ? `<button type="button" class="cmp-open" data-id="${escapeHtml(c.run_id)}">abrir análise completa →</button>`
    : "";
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
          `<span class="chart-zoom-hint">roda=zoom · régua direita=zoom vertical · régua de baixo=zoom horizontal · arrasta=move 2 eixos · 2 cliques=reseta</span>` +
          `<canvas id="cmpChart${slot}" class="cmp-canvas"></canvas>` +
        `</div></div>`
    : "";
  return `<div class="cmp-col">` +
    `<div class="cmp-col-head"><span class="cmp-col-title">${escapeHtml(title)}${dateStr}</span>` +
      `<span class="cmp-col-actions">${reused}${openBtn}</span></div>` +
    `<div class="cmp-verdict-row"><span class="${verdictClass(v)}">${verdictHtml(v)}</span></div>` +
    deg + err +
    chartCard +
    `<div class="cmp-plan md">${renderMarkdown(plan)}</div>` +
    `<div class="cmp-col-foot"><span>${fmtCost(c.cost)} · ${c.elapsed || 0}s</span></div></div>`;
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
  const agrCls = agr === "concordam" ? "agree"
    : (agr === "divergem" ? "diverge" : (agr === "invalido" ? "invalid" : "partial"));
  const agrLabel = agr === "concordam" ? "✅ Concordam"
    : (agr === "divergem" ? "⚠️ Divergem"
      : (agr === "invalido" ? "⛔ Inválido" : "◐ Parcial"));
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

  // Faixa de reanálise também na comparação: o ativo está aberto, então dá pra
  // rerodar (Padrão / Erick / Comparar) sem redigitar direto daqui. Estado do ativo
  // aberto vem do snapshot do compare (TF de referência = lado A, senão B, senão diário).
  _openTicker = snap.ticker || "";
  _openDate = snap.date || "";
  _assetType = snap.asset_type || "";
  _openView = "compare";   // a barra destaca "Comparar" enquanto a comparação está aberta
  // Escada completa: intradiário vale pra ação e cripto (fonte real keyless dos
  // dois; frame sem candle degrada honesto sob demanda). Só o fallback — a fonte
  // da verdade é result.timeframes do backend.
  _timeframes = ["1w", "1d", "4h", "1h", "15m"];
  const cmpTf = (a && (a.verdict_timeframe || a.timeframe)) ||
    (b && (b.verdict_timeframe || b.timeframe)) || "1d";
  _verdictTf = cmpTf;
  _reTf = _timeframes.includes(cmpTf) ? cmpTf : "1d";
  renderReanalyzeBar();

  $("cmpCols").innerHTML = compareColumn(a, "A") + compareColumn(b, "B");
  $("cmpCols").querySelectorAll("button.cmp-open").forEach((btn) =>
    btn.addEventListener("click", () => openRun(btn.dataset.id))
  );
  // desenha os dois gráficos (canvas já no DOM); cada um interativo e independente
  drawCompareChart("cmpChartA", a);
  drawCompareChart("cmpChartB", b);

  mountAskBox($("askCompare"), snap.run_id);

  scrollToOpen(panel);
  loadHistory();
}

// ---- Exportar PDF (imprimir → salvar como PDF) -----------------------------
// SEM lib: usa o print-to-PDF do navegador. Os botões só chamam window.print();
// toda a preparação (abrir relatórios colapsados, nome de arquivo) roda nos
// eventos globais beforeprint/afterprint — assim funciona pelo botão, pelo
// Ctrl+P nativo E pelo page.pdf() do Playwright (que dispara before/afterprint).
let _printRestore = null;

// Nome de arquivo sugerido (o navegador usa o <title>): TradingDegens_MSFT_4h_2026-08-25.
function pdfFileName() {
  const t = (_openTicker || "analise").toUpperCase().replace(/[^A-Z0-9.\-]/g, "");
  const tf = (_verdictTf || _tf || "").replace(/[^A-Za-z0-9]/g, "");
  const d = (_openDate || "").slice(0, 10);
  return ["TradingDegens", t, tf, d].filter(Boolean).join("_");
}

// Painel de análise VISÍVEL agora (resultado único ou comparação). Só um está
// visível por vez; o outro carrega .hidden.
function visibleAnalysisPanel() {
  const cmp = $("comparePanel");
  if (cmp && !cmp.classList.contains("hidden")) return cmp;
  const res = $("resultPanel");
  if (res && !res.classList.contains("hidden")) return res;
  return null;
}

// beforeprint: abre TODOS os <details> do painel visível (pra o PDF conter a
// análise completa), carimba o <title> pro nome do arquivo, e guarda o estado
// pra restaurar no afterprint.
function preparePrint() {
  if (_printRestore) return;                 // já preparado (evento duplo)
  const panel = visibleAnalysisPanel();
  if (!panel) return;                        // nada de análise na tela → imprime como está
  document.body.classList.add("printing");
  const details = Array.from(panel.querySelectorAll("details"));
  const prevOpen = details.map((d) => d.open);
  details.forEach((d) => { d.open = true; });
  const prevTitle = document.title;
  document.title = pdfFileName();
  _printRestore = () => {
    details.forEach((d, i) => { d.open = prevOpen[i]; });
    document.title = prevTitle;
    document.body.classList.remove("printing");
    _printRestore = null;
  };
}

function restorePrint() {
  if (_printRestore) _printRestore();
}

function bindExportPdf() {
  window.addEventListener("beforeprint", preparePrint);
  window.addEventListener("afterprint", restorePrint);
  // matchMedia é o fallback de restauração em navegadores que não emitem
  // afterprint depois do diálogo (garante que os <details> voltem ao estado).
  if (window.matchMedia) {
    const mq = window.matchMedia("print");
    const onChange = (e) => { if (!e.matches) restorePrint(); };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
  const single = $("exportPdfBtn");
  if (single) single.addEventListener("click", () => window.print());
  const cmp = $("exportPdfCmpBtn");
  if (cmp) cmp.addEventListener("click", () => window.print());
}

// ---- Q&A ancorado sobre a análise aberta (task 027) ------------------------
// Uma caixa "pergunte sobre esta análise" atrelada ao run_id. A resposta vem
// ANCORADA nos números da própria run (EMA 8/21, zonas do price_structure) via
// /api/ask — modelo barato, custo à vista, SEM re-rodar. Igual na análise única e
// no confronto (só muda o container). O thread acumula pergunta→resposta enquanto
// o mesmo run está aberto; abrir outro run reconstrói a caixa limpa.
const _askThreads = {};   // run_id -> [{q, a, cost, pending, err}]

function mountAskBox(container, runId) {
  if (!container) return;
  if (!runId) { container.innerHTML = ""; container.dataset.runId = ""; return; }
  // Já montada pro mesmo run: preserva o thread (renderResult pode rodar de novo).
  if (container.dataset.runId === runId && container.firstChild) return;
  container.dataset.runId = runId;
  container.innerHTML =
    `<div class="ask-head">💬 Pergunte sobre esta análise</div>` +
    `<div class="ask-thread"></div>` +
    `<form class="ask-form" autocomplete="off">` +
      `<input type="text" class="ask-input" maxlength="500" ` +
        `placeholder="Pergunte sobre esta análise… (ex.: onde seria o recuo à média?)" ` +
        `aria-label="Pergunte sobre esta análise" />` +
      `<button type="submit" class="ask-send">Perguntar</button>` +
    `</form>` +
    `<p class="ask-hint">Responde com os níveis reais desta análise — não re-roda nada · custo à vista.</p>`;
  const thread = container.querySelector(".ask-thread");
  const form = container.querySelector(".ask-form");
  const input = container.querySelector(".ask-input");
  renderAskThread(thread, runId);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    askQuestion(runId, q, thread, form);
  });
}

function renderAskThread(thread, runId) {
  if (!thread) return;
  const items = _askThreads[runId] || [];
  thread.innerHTML = items.map((it) => {
    const q = `<div class="ask-q"><span class="ask-q-tag">você</span>${escapeHtml(it.q)}</div>`;
    if (it.pending) return q + `<div class="ask-a ask-pending">pensando…</div>`;
    if (it.err) return q + `<div class="ask-a ask-err">${escapeHtml(it.err)}</div>`;
    const cost = it.cost ? `<span class="ask-cost">${fmtCost(it.cost)}</span>` : "";
    return q + `<div class="ask-a"><div class="md">${renderMarkdown(it.a || "")}</div>${cost}</div>`;
  }).join("");
  thread.scrollTop = thread.scrollHeight;
}

async function askQuestion(runId, question, thread, form) {
  const items = _askThreads[runId] || (_askThreads[runId] = []);
  const entry = { q: question, pending: true };
  items.push(entry);
  renderAskThread(thread, runId);
  const btn = form && form.querySelector(".ask-send");
  if (btn) btn.disabled = true;
  try {
    const res = await apiPost("/api/ask", { run_id: runId, question });
    const data = await res.json().catch(() => ({}));
    entry.pending = false;
    if (!res.ok || data.error) {
      entry.err = data.error || "falha ao responder";
    } else {
      entry.a = data.answer || "";
      entry.cost = data.cost;
    }
  } catch (e) {
    entry.pending = false;
    entry.err = "erro de rede — tente de novo";
  } finally {
    if (btn) btn.disabled = false;
    renderAskThread(thread, runId);
  }
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
    const res = await apiPost("/api/compare", { a, b });
    const snap = await res.json();
    if (!res.ok) { $("formError").textContent = snap.error || "falha ao confrontar"; return; }
    // Par Padrão × Erick já pronto (mesmo frame/data): confronto direto, reusa as
    // duas análises. Renderiza na hora.
    if (snap.result && snap.result.compare) { renderCompare(snap); return; }
    // Não era Padrão × Erick no mesmo frame (ex.: dois Padrão) — o backend refez
    // pela via correta, rodando só o método que faltava. Acompanha o run.
    if (snap.run_id) {
      renderProgress({
        status: "running", ticker: snap.ticker || "", elapsed: 0, cost: null,
        progress: { phase: "Inicializando", label: "Refazendo como Padrão × Erick…", percent: 3, plan: [], reached: [] },
      });
      watchRun(snap.run_id);
      loadHistory();
      return;
    }
    $("formError").textContent = "falha ao confrontar";
  } catch (e) { $("formError").textContent = "falha ao confrontar"; }
}

// ---- reanálise com método explícito, sem redigitar (task 023) ---------------
// Com um ativo ABERTO (clicado no histórico ou já na tela), esta faixa oferece o
// ticker JÁ preenchido + escolha de método (Padrão / Erick / Comparar) + timeframe.
// Clicar um método RODA na hora — a comparação fica a ≤2 cliques do ativo escolhido
// (1: abrir o ativo · 2: ⚖️ Comparar). Reusa exatamente os endpoints de /api/analyze.
let _reTf = "1d";   // timeframe escolhido para a próxima reanálise (default = TF do veredito aberto)

function renderReanalyzeBar() {
  const bar = $("reanalyzeBar");
  if (!bar) return;
  if (!_openTicker) { bar.classList.add("hidden"); bar.innerHTML = ""; return; }
  const enabled = new Set(_timeframes || ["1d"]);
  // TF fora da escada operável do ativo cai no do veredito (defesa; hoje ação e
  // cripto têm a escada inteira, mas o backend continua sendo a fonte da verdade).
  if (!enabled.has(_reTf)) _reTf = enabled.has(_verdictTf) ? _verdictTf : "1d";
  const tfBtns = ALL_TFS.map(([tf, label]) => {
    const on = enabled.has(tf);
    const active = tf === _reTf;
    const cls = ["re-tf", active ? "is-active" : "", on ? "" : "is-off"].filter(Boolean).join(" ");
    const title = on ? `Reanalisar no ${label}` : "Frame indisponível para este ativo (o backend não inventa candle)";
    return `<button type="button" class="${cls}" data-retf="${tf}" ${on ? "" : "disabled"} title="${escapeHtml(title)}">${escapeHtml(label)}</button>`;
  }).join("");
  // O método aberto ganha destaque (is-open): é o antigo "Atualizar" embutido —
  // clicá-lo reanalisa HOJE preservando o método. Padrão/Erick/Comparar num lugar só.
  const methods = [
    ["padrao", "Padrão", "Reanalisa com a leitura Padrão no timeframe escolhido, na data de hoje"],
    ["erick", "🧭 Erick", "Reanalisa com o método Erick — recuo à média, saída antes da reversão, peso do trade — na data de hoje"],
    ["compare", "⚖️ Comparar", "Roda as DUAS (Padrão e Erick) e confronta com o meta-juiz — a divergência é o sinal"],
  ];
  const mBtns = methods.map(([m, label, title]) => {
    const open = m === _openView;
    const cls = ["re-method", m, open ? "is-open" : ""].filter(Boolean).join(" ");
    const t = open ? `${title} · leitura aberta agora — clique = atualizar hoje` : title;
    return `<button type="button" class="${cls}" data-method="${m}"${open ? ' aria-current="true"' : ""} title="${escapeHtml(t)}">${escapeHtml(label)}</button>`;
  }).join("");
  bar.innerHTML =
    `<div class="re-lead"><span class="re-icon">🔁</span>Reanalisar <b class="re-ticker">${escapeHtml(_openTicker)}</b> <span class="re-today">hoje</span></div>` +
    `<div class="re-grp">` +
      `<span class="re-glabel">tempo</span>` +
      `<div class="re-tfs" role="group" aria-label="Timeframe da reanálise">${tfBtns}</div>` +
    `</div>` +
    `<div class="re-grp re-grp-methods">` +
      `<span class="re-glabel">método</span>` +
      `<div class="re-methods">${mBtns}</div>` +
    `</div>`;
  bar.classList.remove("hidden");
}

function bindReanalyzeBar() {
  const bar = $("reanalyzeBar");
  if (!bar || bar._bound) return;
  bar._bound = true;
  bar.addEventListener("click", (e) => {
    const tfBtn = e.target.closest("button.re-tf");
    if (tfBtn && !tfBtn.disabled) { _reTf = tfBtn.dataset.retf; renderReanalyzeBar(); return; }
    const mBtn = e.target.closest("button.re-method");
    if (mBtn) runReanalyze(mBtn.dataset.method, _reTf);
  });
}

// Dispara a reanálise do ativo ABERTO sem redigitar: método explícito + TF escolhido,
// dados de hoje (mesma semântica de "Atualizar"). Reusa /api/analyze (method|compare +
// timeframe) — o mesmo caminho do launcher, sem fluxo paralelo que possa divergir.
function runReanalyze(method, tf) {
  if (!_openTicker) return;
  const compare = method === "compare";
  const m = method === "erick" ? "erick" : "padrao";
  const date = _todayManaus || _openDate || "";
  $("formError").textContent = "";
  $("resultPanel").classList.add("hidden");
  $("comparePanel").classList.add("hidden");
  $("steps").innerHTML = "";
  const boot = compare
    ? "Comparando Padrão × Erick…"
    : (m === "erick" ? "Método Erick — subindo o motor…" : "Subindo o motor…");
  renderProgress({
    status: "running", ticker: _openTicker, elapsed: 0, cost: null,
    progress: { phase: "Inicializando", label: boot, percent: 2, plan: [], reached: [] },
  });
  apiPost("/api/analyze", { ticker: _openTicker, date, method: m, compare, timeframe: tf || "1d" })
    .then((r) => r.json())
    .then((data) => {
      if (data && data.run_id) { watchRun(data.run_id); loadHistory(); }
      else { $("formError").textContent = (data && data.error) || "falha ao reanalisar"; }
    })
    .catch(() => { $("formError").textContent = "falha ao reanalisar"; });
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
let _timeframes = ["1d"];     // frames operáveis do ativo aberto (ação e cripto = escada inteira)
let _openDate = "";           // data da análise aberta (recomputa por timeframe)
let _assetType = "";          // tipo do ativo aberto (define a fonte do intradiário)
let _verdictTf = "1d";        // timeframe em que o veredito ABERTO foi computado (carimbo)

// paddings do gráfico, compartilhados entre o desenho e a interação de zoom/pan
// (o zoom vertical precisa converter y do cursor → preço com a MESMA geometria)
const PAD_L = 8, PAD_R = 58, PAD_T = 12, PAD_B = 22;
// Respiro horizontal entre a última vela e a régua/pílulas do eixo direito (task
// 033): as velas terminam PLOT_RIGHT_GAP px antes do eixo, então a pílula de preço
// não cola no candle. As linhas de nível ainda cruzam até o eixo. O mapeamento
// x/zoom/arrasto usa a largura reduzida (plotW - PLOT_RIGHT_GAP) pra continuar certo.
const PLOT_RIGHT_GAP = 16;

// Todos os frames do seletor, na ordem exibida. Um frame fora de `_timeframes`
// é renderizado DESABILITADO — o backend nunca inventa candle. Hoje ação e cripto
// têm a escada inteira operável; o mecanismo de desabilitar segue valendo como
// defesa caso o backend devolva uma escada reduzida.
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
// O semanal é reamostrado do diário; o intradiário (4h/1h/15m) vale pra ação e
// cripto — cada frame tem fonte real keyless (exchange na cripto, yfinance na
// ação) e degrada honesto sob demanda quando não há candle pra aquele símbolo/data.
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
      : "Frame indisponível para este ativo (o backend não inventa candle)";
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
// só o TF muda. Reusa o método da análise ABERTA (_openMethod).
function reevaluate(tf) {
  if (!_openTicker) return;
  // O método da reavaliação é o da análise ABERTA (_openMethod): abrir uma Erick pelo
  // histórico/confronto tem que continuar Erick ao reavaliar (aqui e no reavaliar-com-
  // fontes) — mesma verdade que a troca de TF do gráfico (switchTimeframe usa _openMethod).
  // Com uma análise aberta _openMethod sempre existe; cai em 'padrao' só na ausência dela.
  const method = _openMethod || "padrao";
  $("resultPanel").classList.add("hidden");
  $("steps").innerHTML = "";
  renderProgress({
    status: "running", ticker: _openTicker, elapsed: 0, cost: null,
    progress: { phase: "Inicializando", label: `Reavaliando no ${TF_LABEL[tf] || tf}…`, percent: 2, plan: [], reached: [] },
  });
  apiPost("/api/analyze", { ticker: _openTicker, date: _openDate || "", method, timeframe: tf })
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
    const q = new URLSearchParams({ ticker: _openTicker, date: _openDate || "", tf, method: _openMethod || "padrao" });
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

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

// Empilha as pílulas do eixo pra que não se sobreponham quando os níveis ficam
// perto (a Quantfury faz igual): ordena por y, garante um vão mínimo empurrando pra
// baixo e, se estourar a base, comprime de volta pra cima. ``ry`` é o y DESENHADO
// da pílula (pode diferir do nível real ``y``, e aí um leader curto religa os dois).
function layoutAxisPills(pills, top, bottom, gap) {
  if (!pills.length) return;
  pills.forEach((p) => { p.ry = p.y; });
  pills.sort((a, b) => a.ry - b.ry);
  for (let i = 1; i < pills.length; i++) {
    if (pills[i].ry < pills[i - 1].ry + gap) pills[i].ry = pills[i - 1].ry + gap;
  }
  const last = pills.length - 1;
  if (pills[last].ry > bottom) {
    pills[last].ry = bottom;
    for (let i = last - 1; i >= 0; i--) {
      if (pills[i].ry > pills[i + 1].ry - gap) pills[i].ry = pills[i + 1].ry - gap;
    }
  }
  pills.forEach((p) => { p.ry = Math.max(top, Math.min(p.ry, bottom)); });
}

// Uma pílula no eixo Y direito (dentro da régua, largura ≤ gutter): o número do
// nível no y ``ry``, cor da função. ``strong`` = preço atual (contraste claro).
function drawAxisPill(ctx, axisX, gutter, ry, text, bg, fg, strong) {
  ctx.font = "bold 10.5px ui-monospace, Menlo, monospace";
  const padX = 5, h = 15;
  const w = Math.min(ctx.measureText(text).width + padX * 2, gutter - 2);
  const x0 = axisX + 1;
  roundRect(ctx, x0, ry - h / 2, w, h, 4);
  ctx.fillStyle = bg; ctx.globalAlpha = strong ? 1 : 0.95; ctx.fill(); ctx.globalAlpha = 1;
  if (strong) { ctx.strokeStyle = "rgba(0,0,0,0.28)"; ctx.lineWidth = 1; ctx.stroke(); }
  ctx.fillStyle = fg; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(text, x0 + padX, ry + 0.5);
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
  // fundo do gráfico PRETO PURO (Quantfury, task 029) — nada de navy; velas, médias
  // e as pílulas do eixo contrastam sobre ele.
  ctx.fillStyle = "#000000";
  ctx.fillRect(0, 0, cssW, cssH);

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
  // escala vertical observável (px por unidade de preço): sobe quando o zoom v
  // comprime a janela — é o que estica corpo/pavio das velas na altura.
  canvas.dataset.ppp = plotH / (hi - lo);

  // Velas/médias/marcadores ocupam a largura MENOS o respiro à direita (plotWx),
  // deixando um gap até a régua; gridlines/bandas/linhas de nível ainda vão até o
  // eixo (padL + plotW) pra conectar nas pílulas.
  const plotWx = Math.max(1, plotW - PLOT_RIGHT_GAP);
  const x = (i) => padL + (i - v0 + 0.5) * (plotWx / vis);
  const y = (p) => padT + (1 - (p - lo) / (hi - lo)) * plotH;

  // Etiquetas de nível na RÉGUA DA DIREITA (estilo Quantfury): o preço atual e as
  // zonas viram pílulas coloridas no eixo Y, na altura exata do nível — a linha
  // horizontal (desenhada adiante) cruza o gráfico até elas. Nada de caixa tampando
  // vela. Montadas aqui pra que os números de grade que caírem sob uma pílula sejam
  // omitidos (a pílula manda). O empilhamento (não colar quando os níveis estão
  // perto) é resolvido em layoutAxisPills.
  const axisX = padL + plotW;
  const pillH = 15;
  const fmtAxis = (v) => Number(v).toLocaleString("pt-BR", {
    minimumFractionDigits: Math.abs(v) < 1000 ? 2 : 0,
    maximumFractionDigits: Math.abs(v) < 1000 ? 2 : 0,
  });
  const axisPills = [];
  if (price != null) {
    axisPills.push({ y: y(price), text: fmtAxis(price), bg: "#e6eaf2", fg: "#000000", strong: true });
  }
  // Cada ZONA mostra o RANGE na régua: DUAS pílulas (máx e mín da banda) na altura
  // exata de cada ponta — o Samyr lê a faixa inteira sem cobrir vela (a sombra da
  // faixa continua). Sem banda (sem base de ATR) degrada pra uma pílula no nível.
  zones.forEach((z) => {
    const hasBand = z.low != null && z.high != null && z.high > z.low;
    if (hasBand) {
      axisPills.push({ y: y(z.high), text: fmtAxis(z.high), bg: z.color, fg: "#000000" });
      axisPills.push({ y: y(z.low), text: fmtAxis(z.low), bg: z.color, fg: "#000000" });
    } else if (z.price != null) {
      axisPills.push({ y: y(z.price), text: fmtAxis(z.price), bg: z.color, fg: "#000000" });
    }
  });
  if (pat) {
    axisPills.push({ y: y(pat.trigger), text: fmtAxis(pat.trigger), bg: patColor(pat), fg: "#000000" });
  }
  layoutAxisPills(axisPills, padT + pillH / 2 + 1, padT + plotH - pillH / 2 - 1, pillH + 2);
  const pillCovers = (yy) => axisPills.some((p) => Math.abs(p.ry - yy) < pillH);

  // gridlines + price labels (y axis, right) — o número de grade some onde uma
  // pílula de nível ocupa a linha (senão ficariam dois números sobrepostos).
  ctx.font = "11px ui-monospace, Menlo, monospace";
  ctx.textBaseline = "middle";
  const ticks = 5;
  for (let t = 0; t <= ticks; t++) {
    const p = lo + (hi - lo) * (t / ticks);
    const yy = y(p);
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
    if (!pillCovers(yy)) {
      ctx.fillStyle = "#8b97ad"; ctx.textAlign = "left";
      ctx.fillText(p.toLocaleString("pt-BR", { maximumFractionDigits: p < 10 ? 2 : 0 }), padL + plotW + 6, yy);
    }
  }

  // timeframe stamp — o frame do padrão fica escrito NO gráfico (não só no card),
  // pra ninguém confundir um 1-2-3 de 15m com o do diário.
  const tfText = TF_LABEL[chart.timeframe] || chart.timeframe || "Diário";
  ctx.font = "bold 11px ui-monospace, Menlo, monospace";
  const tfW = ctx.measureText(tfText).width + 14;
  roundRect(ctx, padL + 2, padT + 2, tfW, 17, 4);
  ctx.globalAlpha = 0.85; ctx.fillStyle = "#111111"; ctx.fill(); ctx.globalAlpha = 1;
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

  // candles — a LARGURA distorce com o zoom h: acompanha o passo por vela
  // (plotWx/vis) a 70%, então poucas velas na janela = velas largas, muitas = finas.
  // Piso de 1px pra não sumir no zoom-out extremo; SEM teto (o teto travava a
  // distorção ao aproximar). A ALTURA distorce sozinha via y() (usa _vview/_yGeom):
  // ao comprimir o eixo, a mesma variação open→close/high→low ocupa mais pixels.
  const cw = Math.max(1, (plotWx / vis) * 0.7);
  canvas.dataset.cw = cw;   // largura da vela observável (distorção h)
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
        ctx.fillStyle = "#000000"; ctx.beginPath(); ctx.arc(px, cy + off, 8, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = col; ctx.stroke();
        ctx.fillStyle = col; ctx.fillText(p.lab, px, cy + off);
        ctx.font = "10px ui-monospace, Menlo, monospace"; ctx.fillStyle = "#8b97ad";
        ctx.fillText(fmtNum(p.price), px, cy + off + (p.kind === "L" ? 16 : -16));
      });
      // o número do gatilho vai pra pílula no eixo direito (junto com preço/zonas);
      // aqui fica só a linha do 1-2-3 e os pontos numerados na vela.
    }
  }

  // linha fina do preço atual cruzando o gráfico até a régua direita (o número
  // vira pílula no eixo, logo abaixo — nada de caixa sobre as velas).
  if (price != null) {
    const yp = y(price);
    ctx.strokeStyle = "rgba(230,234,242,0.5)"; ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, yp); ctx.lineTo(padL + plotW, yp); ctx.stroke();
    ctx.setLineDash([]);
  }

  // pílulas de nível na RÉGUA DIREITA (por último, por cima dos números de grade):
  // preço atual (destaque claro), zonas (cor da função) e gatilho 1-2-3. Quando o
  // nível foi deslocado pra não colar, um leader curto liga o y REAL à pílula.
  axisPills.forEach((p) => {
    if (Math.abs(p.ry - p.y) > 1) {
      ctx.strokeStyle = p.bg; ctx.globalAlpha = 0.5; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(axisX, p.y); ctx.lineTo(axisX + 5, p.ry); ctx.stroke();
      ctx.globalAlpha = 1;
    }
    drawAxisPill(ctx, axisX, padR, p.ry, p.text, p.bg, p.fg, p.strong);
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
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left - PAD_L) / (rect.width - PAD_L - PAD_R - PLOT_RIGHT_GAP)));
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
  let vzoom = null;   // zoom VERTICAL: arrastar na régua de preço (eixo direito) escala _vview
  let hzoom = null;   // zoom HORIZONTAL: arrastar na régua de tempo (eixo de baixo) escala _view
  const VZOOM_SENS = 2.6;   // sensibilidade do arraste-na-régua vertical (altura cheia ~ fator e^±2.6)
  const HZOOM_SENS = 2.6;   // sensibilidade do arraste-na-régua horizontal (largura cheia ~ e^±2.6)
  const fracX = (clientX) => {
    const rect = canvas.getBoundingClientRect();
    return Math.min(1, Math.max(0, (clientX - rect.left - PAD_L) / (rect.width - PAD_L - PAD_R - PLOT_RIGHT_GAP)));
  };
  canvas.addEventListener("pointerdown", (e) => {
    if (!N()) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      const { v0, v1 } = cur();
      pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, v0, v1, frac: fracX((a.x + b.x) / 2) };
      drag = null; vzoom = null; hzoom = null;   // 2 dedos = pinça; solta os gestos de 1 ponteiro
    } else if (pts.size === 1) {
      const rect = canvas.getBoundingClientRect();
      const inAxis = (e.clientX - rect.left) >= (rect.width - PAD_R - 2);   // régua de preço (direita)
      const inXAxis = (e.clientY - rect.top) >= (rect.height - PAD_B - 2);  // régua de tempo (baixo)
      if (inAxis && canvas._yGeom) {
        // arrastar na régua de preço = ZOOM vertical (comprime/expande _vview em torno
        // do centro). Cima = aproxima (velas crescem); baixo = afasta. Como TradingView.
        // Prioridade: régua direita ganha do canto inferior-direito (evita conflito).
        vzoom = { y: e.clientY, lo: canvas._yGeom.lo, hi: canvas._yGeom.hi };
        canvas.style.cursor = "ns-resize";
        drag = null;
      } else if (inXAxis) {
        // arrastar na régua de tempo = ZOOM horizontal (comprime/expande _view em torno
        // do centro). Esquerda = aproxima (menos candles, mais largos); direita = afasta.
        const { v0, v1 } = cur();
        hzoom = { x: e.clientX, v0, v1 };
        canvas.style.cursor = "ew-resize";
        drag = null;
      } else if (canvas._view || canvas._vview) {
        // corpo do gráfico com algum zoom ativo = PAN livre 2D (h e/ou v juntos, mantendo o zoom).
        drag = {
          x: e.clientX, y: e.clientY,
          v: canvas._view ? { v0: canvas._view.v0, v1: canvas._view.v1 } : null,
          vv: canvas._vview ? { lo: canvas._vview.lo, hi: canvas._vview.hi } : null,
        };
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
    if (vzoom) {
      const rect = canvas.getBoundingClientRect();
      const plotH = Math.max(1, rect.height - PAD_T - PAD_B);
      const range = vzoom.hi - vzoom.lo;
      const center = (vzoom.lo + vzoom.hi) / 2;               // âncora = centro do range agarrado
      const dy = e.clientY - vzoom.y;                         // >0 arrasta pra baixo
      const factor = Math.exp(dy / plotH * VZOOM_SENS);       // cima<0=comprime; baixo>0=expande
      const nr = range * factor;
      const autoRange = canvas._autoY ? (canvas._autoY.hi - canvas._autoY.lo) : range;
      if (nr >= autoRange) {
        canvas._vview = null;                                 // expandiu além do auto = solta pra autoescala
      } else {
        const r = Math.max(nr, Math.max(autoRange * 0.01, 1e-9));  // piso: não colapsa a escala
        canvas._vview = { lo: center - r / 2, hi: center + r / 2 };
      }
      redraw();
      return;
    }
    if (hzoom) {
      const rect = canvas.getBoundingClientRect();
      const plotW = Math.max(1, rect.width - PAD_L - PAD_R - PLOT_RIGHT_GAP);
      const vis0 = hzoom.v1 - hzoom.v0;
      const center = (hzoom.v0 + hzoom.v1) / 2;               // âncora = centro da janela agarrada
      const dx = e.clientX - hzoom.x;                         // <0 arrasta pra esquerda
      const factor = Math.exp(dx / plotW * HZOOM_SENS);       // esquerda<0=comprime; direita>0=expande
      let nv = Math.max(8, Math.min(N(), Math.round(vis0 * factor)));  // piso ~8 candles; teto = total
      if (nv >= N()) {
        canvas._view = null;                                  // expandiu além do total = visão cheia
      } else {
        const nv0 = Math.max(0, Math.min(Math.round(center - nv / 2), N() - nv));
        canvas._view = { v0: nv0, v1: nv0 + nv };
      }
      redraw();
      return;
    }
    if (drag) {
      const rect = canvas.getBoundingClientRect();
      // PAN horizontal: desliza a janela de candles (só quando há zoom h ativo)
      if (drag.v) {
        const vis = drag.v.v1 - drag.v.v0;
        const dC = Math.round((e.clientX - drag.x) / (rect.width - PAD_L - PAD_R - PLOT_RIGHT_GAP) * vis);
        const nv0 = Math.max(0, Math.min(drag.v.v0 - dC, N() - vis));
        canvas._view = { v0: nv0, v1: nv0 + vis };
      }
      // PAN vertical: desliza a janela de preço (só quando há zoom v ativo). dy>0
      // (arrasta pra baixo) sobe a janela → o preço agarrado acompanha o cursor.
      if (drag.vv) {
        const plotH = Math.max(1, rect.height - PAD_T - PAD_B);
        const range = drag.vv.hi - drag.vv.lo;
        const dPrice = (e.clientY - drag.y) / plotH * range;
        canvas._vview = { lo: drag.vv.lo + dPrice, hi: drag.vv.hi + dPrice };
      }
      redraw();
    }
  });
  const drop = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (pts.size === 0) { drag = null; vzoom = null; hzoom = null; canvas.style.cursor = (canvas._view || canvas._vview) ? "grab" : "default"; }
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
  saveActiveRun(runId, _openTicker);   // persiste pra reengatar após refresh / 2º plano
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
      saveActiveRun(runId, snap.ticker);   // mantém o ticker fresco pro reengate
      renderProgress(snap);
    } else {
      clearActiveRun();                    // terminou: não há mais o que reengatar
      renderResult(snap);
    }
  } catch (e) {
    // erro transitório de rede: mantém o timer (segue tentando) e o estado persistido
  }
}

// Reengate ao CARREGAR a página (e ao voltar de 2º plano): se havia um run sendo
// acompanhado e ele ainda está `running` no servidor, volta a mostrar o progresso
// ao vivo — o front não "esquece" a análise. Se terminou enquanto estávamos fora,
// abre o resultado e limpa. Rede fora: mantém o estado (tenta de novo depois).
// Retorna true se assumiu a tela (aí o openLatestRun não precisa rodar).
async function resumeActiveRun() {
  const active = loadActiveRun();
  if (!active || !active.run_id) return false;
  try {
    const res = await fetch("/api/status/" + active.run_id);
    if (res.status === 404) { clearActiveRun(); return false; }   // run sumiu do servidor
    if (!res.ok) return false;                                    // transitório: tenta depois
    const snap = await res.json();
    if (snap.status === "running") {
      _openTicker = snap.ticker || active.ticker || "";
      if (snap.ticker) $("ticker").value = snap.ticker;
      $("resultPanel").classList.add("hidden");
      $("comparePanel").classList.add("hidden");
      renderProgress(snap);
      watchRun(active.run_id);
      return true;
    }
    clearActiveRun();        // terminou em 2º plano: mostra o resultado final e limpa
    renderResult(snap);
    return true;
  } catch (e) {
    return false;            // offline no boot: deixa o openLatestRun assumir a tela
  }
}

async function startAnalysis(ev) {
  ev.preventDefault();
  $("formError").textContent = "";
  const ticker = $("ticker").value.trim();
  const date = $("date").value;
  if (!ticker) { $("formError").textContent = "Informe um ticker."; return; }
  // O launcher só ABRE o ativo: roda sempre Padrão (o método vive na barra de
  // reanálise, um lugar só). Sem checkbox aqui → nada de método fantasma no POST.
  const method = "padrao";
  const compare = false;
  $("runBtn").disabled = true;
  $("resultPanel").classList.add("hidden");
  $("comparePanel").classList.add("hidden");
  $("steps").innerHTML = "";
  resetThinking();   // análise nova: começa com o painel de raciocínio limpo
  try {
    const res = await apiPost("/api/analyze", { ticker, date, method, compare });
    const data = await res.json();
    if (res.status === 403 && data.error_code === "need_key") {
      handleNeedKey(data.error);
      return;
    }
    if (!res.ok) throw new Error(data.error || "falha ao iniciar");
    renderProgress({ status: "running", ticker, elapsed: 0, cost: null, progress: { phase: "Inicializando", label: "Subindo o motor…", percent: 2, plan: [], reached: [] } });
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

// "Atualizar" (reanalisar hoje preservando o método) foi ABSORVIDO pela barra de
// reanálise (task 018): o método aberto fica destacado (is-open) e clicá-lo roda na
// data de hoje via runReanalyze() — a ÚNICA função de "rodar o ativo aberto". Sem
// botão separado que re-submetesse o form e perdesse o método (a classe do bug 037/039).
let _allRuns = [];

// Preço LIVE da 3ª linha da watchlist (task 010): SÍMBOLO -> {price, change_pct,
// currency} | null. Persistido entre repinturas da lista pra a linha NÃO piscar
// "—" a cada refresh de 5s — o cache guarda o último preço e a lista repinta com
// ele; o poller de preço atualiza só os spans no lugar (sem dança).
const _priceCache = new Map();
let _priceTimer = null;
let _priceFetching = false;

// Formata o preço conforme a magnitude (2 casas ≥1; mais casas pra fração de dólar)
// com separador de milhar; "$" pra USD/desconhecido, senão o código da moeda.
function fmtPrice(v, currency) {
  const usd = !currency || currency === "USD";
  const a = Math.abs(v);
  const digits = a >= 1 ? 2 : (a >= 0.01 ? 4 : 6);
  const num = v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return usd ? `$${num}` : `${num} ${escapeHtml(currency)}`;
}

// Linha de preço: valor + variação do dia (▲ verde / ▼ vermelho). Sem dado → "—".
function priceLineHtml(p) {
  if (!p || p.price == null) return `<span class="pdash">—</span>`;
  let chg = "";
  if (p.change_pct != null) {
    const up = p.change_pct > 0, dn = p.change_pct < 0;
    const cls = up ? "up" : (dn ? "down" : "flat");
    const arrow = up ? "▲" : (dn ? "▼" : "·");
    chg = ` <span class="pchg ${cls}">${arrow} ${Math.abs(p.change_pct).toFixed(2)}%</span>`;
  }
  return `<span class="pval">${fmtPrice(p.price, p.currency)}</span>${chg}`;
}

function currentHistoryTickers() {
  return [...document.querySelectorAll(".history li[data-ticker]")]
    .map((li) => li.getAttribute("data-ticker"))
    .filter(Boolean);
}

// Busca o preço live dos tickers visíveis e aplica NOS SPANS, sem repintar a lista
// (evita a "dança"). Reusa o cache do servidor (~45s) — chamadas repetidas são baratas.
async function refreshPrices(tickers) {
  const src = tickers && tickers.length ? tickers : currentHistoryTickers();
  const uniq = [...new Set(src.map((t) => (t || "").toUpperCase()).filter(Boolean))];
  if (!uniq.length || _priceFetching) return;
  _priceFetching = true;
  try {
    const res = await fetch("/api/prices?tickers=" + encodeURIComponent(uniq.join(",")));
    if (!res.ok) return;
    const data = await res.json();
    const prices = data.prices || {};
    Object.keys(prices).forEach((k) => _priceCache.set(k.toUpperCase(), prices[k]));
    document.querySelectorAll(".history .h-price[data-price-for]").forEach((el) => {
      const t = (el.getAttribute("data-price-for") || "").toUpperCase();
      if (_priceCache.has(t)) el.innerHTML = priceLineHtml(_priceCache.get(t));
    });
  } catch (e) { /* fonte fora do ar: mantém "—" ou o último preço em cache */ }
  finally { _priceFetching = false; }
}

// Só busca os tickers AINDA sem preço em cache (novos na lista) — não re-bate a
// fonte a cada repintura de 5s; o poller periódico cuida de atualizar os existentes.
function refreshNewPrices() {
  const missing = currentHistoryTickers().filter((t) => !_priceCache.has((t || "").toUpperCase()));
  if (missing.length) refreshPrices(missing);
}

function startPriceAutoRefresh() {
  if (_priceTimer) clearInterval(_priceTimer);
  // atualiza os preços a cada 40s, só com a aba em primeiro plano (aba de fundo não
  // martela a fonte); o retorno ao foco força um refresh imediato (onVisibleForeground).
  _priceTimer = setInterval(() => {
    if (document.visibilityState === "visible") refreshPrices();
  }, 40000);
}

function bindHistoryTabs() {
  const tabs = document.getElementById("historyTabs");
  if (!tabs) return;
  tabs.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".h-tab");
    if (!btn) return;
    _historyFilter = btn.dataset.filter || "all";
    tabs.querySelectorAll(".h-tab").forEach((b) => b.classList.toggle("is-active", b === btn));
    paintHistory();   // troca de aba re-pinta do cache, sem re-buscar o histórico
  });
}

// Nome da empresa/ativo por símbolo — TICKER ( Nome ). Resolve async via /api/names
// (Yahoo search, cacheado no backend); aqui um cache de sessão evita re-perguntar.
// "" = resolvido-vazio (fonte não trouxe nome) → mostra só o ticker, nunca inventa.
const _nameCache = new Map();   // SÍMBOLO(MAIÚSCULA) -> nome

async function ensureNames(tickers) {
  const need = [...new Set((tickers || []).map((t) => (t || "").toUpperCase()).filter(Boolean))]
    .filter((t) => !_nameCache.has(t));
  if (!need.length) return false;
  try {
    const res = await fetch("/api/names?symbols=" + encodeURIComponent(need.join(",")));
    const data = await res.json();
    const names = data.names || {};
    need.forEach((t) => _nameCache.set(t, names[t] || ""));
  } catch (e) {
    need.forEach((t) => _nameCache.set(t, ""));   // fonte fora do ar = só ticker
  }
  return true;
}

function tickerLabelHtml(ticker) {
  const t = (ticker || "").toUpperCase();
  const n = _nameCache.get(t);
  const sym = `<span class="tk-sym">${escapeHtml(t || "?")}</span>`;
  return n ? `${sym} <span class="tk-co">( ${escapeHtml(n)} )</span>` : sym;
}

async function loadHistory() {
  try {
    const res = await fetch("/api/history");
    const data = await res.json();
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

    _allRuns = runs;
    paintHistory();
  } catch (e) { /* ignore */ }
}

// Pinta a lista lateral a partir de `_allRuns` (sem re-buscar): usada pelo refresh,
// pela troca de aba e pela re-pintura quando os nomes resolvem async.
function paintHistory() {
  const ul = $("history");
  if (!ul) return;
  const runs = _allRuns || [];
  if (!runs.length) { ul.innerHTML = '<li class="empty">Nenhuma análise ainda.</li>'; return; }
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
      // Watchlist densa (task 009): a coluna estreita da lateral só comporta
      // veredito + DATA à direita sem espremer o nome da empresa. Custo/tempo
      // seguem visíveis no cabeçalho da análise aberta (Custo/Tempo), não aqui.
      meta = r.finished_at ? fmtStamp(r.finished_at) : escapeHtml(r.date || "");
    }
    // watchlist: ticker em negrito + nome cinza (2 linhas) à esquerda; veredito +
    // meta empilhados à direita; × discreto pra remover (só em run já concluído —
    // não se remove uma análise em andamento).
    const t = (r.ticker || "").toUpperCase();
    const co = _nameCache.get(t);
    const coHtml = co ? `<span class="tk-co">${escapeHtml(co)}</span>` : "";
    const rm = running ? "" :
      `<button type="button" class="h-remove" data-ticker="${escapeHtml(t)}" ` +
      `title="Remover ${escapeHtml(t)} do histórico" aria-label="Remover ${escapeHtml(t)}">×</button>`;
    // 3ª linha (task 010): preço LIVE do ativo (+ variação do dia). Nasce do cache
    // (ou "—") e o poller de preço atualiza no lugar. Cripto e ação.
    const priceHtml = `<span class="h-price" data-price-for="${escapeHtml(t)}">${priceLineHtml(_priceCache.get(t))}</span>`;
    return `<li data-id="${escapeHtml(r.run_id)}" data-ticker="${escapeHtml(t)}" class="${running ? "is-running" : ""}">` +
      `<span class="h-ticker">` +
        `<span class="h-sym"><span class="tk-sym">${escapeHtml(t || "?")}</span>${badge}${flagHtml}</span>` +
        coHtml +
      `</span>` +
      `<span class="h-right">` +
        `<span class="h-verdict ${vClass}" title="${escapeHtml(running ? "em andamento" : v)}">${vHtml}</span>` +
        `<span class="h-meta">${meta}</span>` +
      `</span>` +
      rm +
      priceHtml +
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
    const rm = li.querySelector(".h-remove");
    if (rm) rm.addEventListener("click", (ev) => {
      ev.stopPropagation();   // não abrir a análise ao clicar no ×
      removeTicker(rm.getAttribute("data-ticker"));
    });
  });
  // Resolve os nomes que faltam e re-pinta UMA vez quando chegam (cacheado → o
  // segundo ensureNames não muda nada e o loop para).
  ensureNames([...seen.keys()]).then((changed) => { if (changed) paintHistory(); });
  // Busca o preço live dos tickers NOVOS (ainda sem cache) — a 3ª linha sai de "—"
  // pro preço assim que a lista aparece; os existentes seguem no poller de 40s.
  refreshNewPrices();
}

// Remove um ATIVO da lista lateral (a linha é por ticker): apaga do histórico
// todas as análises salvas daquele ativo. Confirma antes (é destrutivo) e re-carrega.
async function removeTicker(ticker) {
  const t = (ticker || "").toUpperCase();
  if (!t) return;
  if (!confirm(`Remover ${t} do histórico? As análises salvas deste ativo serão apagadas.`)) return;
  try {
    const res = await fetch("/api/history/" + encodeURIComponent(t), { method: "DELETE" });
    if (res.ok) await loadHistory();
  } catch (e) { /* fonte fora do ar: mantém a lista como está */ }
}

async function openRun(runId) {
  try {
    _finishedFlags.delete(runId);          // abriu: some o marcador "pronto/erro"
    const res = await fetch("/api/run/" + runId);
    const snap = await res.json();
    if (!res.ok) return;
    // selecionar o ativo pré-preenche o ticker no launcher também (zero redigitação)
    if (snap.ticker) $("ticker").value = snap.ticker;
    if (snap.status === "running") {
      // reabre um run EM ANDAMENTO: volta a acompanhar ao vivo (re-liga o polling
      // daquele run_id). O run nunca parou — só a visão tinha saído dele.
      _openTicker = snap.ticker || "";
      $("resultPanel").classList.add("hidden");
      resetThinking();   // trocou pra outro run em andamento: raciocínio começa limpo
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
    const res = await fetch("/api/config", { credentials: "same-origin" });
    const cfg = await res.json();
    if (cfg.tz_label) TZ_LABEL = cfg.tz_label;
    if (cfg.today) { _todayManaus = cfg.today; $("date").value = cfg.today; }
    $("tzLabel").textContent = "(" + TZ_LABEL + ")";
    $("tzNote").textContent = "Horários em " + TZ_LABEL + ".";
    _isOwner = !!cfg.owner;
    _ownerLoginEnabled = !!cfg.owner_login_enabled;
    if (cfg.llm) { _llmMeta = cfg.llm; renderConfigPanel(); }
    updateConfigBadge();
  } catch (e) {
    // fallback: browser-local date if the server is unreachable at boot
    $("date").value = new Date().toLocaleDateString("en-CA");
  }
}

// Autocomplete do campo ATIVO: busca por sigla OU nome (/api/search, Yahoo keyless).
// Debounce curto pra não bater a cada tecla; preenche o <datalist> com SÍMBOLO —
// Nome. Fonte fora do ar = sem sugestão (o campo segue aceitando o que foi digitado;
// nome vira símbolo no servidor ao Analisar). Nunca bloqueia.
let _suggestTimer = null;
let _suggestSeq = 0;
function scheduleTickerSuggest(term) {
  clearTimeout(_suggestTimer);
  const q = (term || "").trim();
  if (q.length < 2) { const dl = $("tickerSuggest"); if (dl) dl.innerHTML = ""; return; }
  _suggestTimer = setTimeout(() => fetchTickerSuggest(q), 220);
}
async function fetchTickerSuggest(term) {
  const seq = ++_suggestSeq;
  try {
    const res = await fetch("/api/search?q=" + encodeURIComponent(term));
    const data = await res.json();
    if (seq !== _suggestSeq) return;   // resposta velha: uma mais nova já saiu
    const dl = $("tickerSuggest");
    if (!dl) return;
    const results = (data.results || []).slice(0, 8);
    // value = SÍMBOLO (é o que o campo recebe ao escolher); label mostra o nome +
    // seed do cache de nomes, pra o cabeçalho/chip já sair com o nome.
    dl.innerHTML = results.map((r) => {
      const sym = (r.symbol || "").toUpperCase();
      if (sym && r.name && !_nameCache.has(sym)) _nameCache.set(sym, r.name);
      const label = r.name ? `${sym} — ${r.name}` : sym;
      return `<option value="${escapeHtml(r.symbol || "")}" label="${escapeHtml(label)}"></option>`;
    }).join("");
  } catch (e) { /* fonte fora do ar: segue sem sugestão */ }
}

// ---- BYOK: painel de configuração (provedor + chave) ------------------------
function _providerMeta(id) {
  const list = (_llmMeta && _llmMeta.providers) || [];
  return list.find((p) => p.id === id) || null;
}

// Preenche o <select> de provedores e reflete a config salva nos campos.
function renderConfigPanel() {
  const sel = $("cfgProvider");
  if (!sel || !_llmMeta) return;
  const list = _llmMeta.providers || [];
  const cur = _llmCfg.provider || _llmMeta.default_provider || "openai";
  sel.innerHTML = list.map((p) =>
    `<option value="${escapeHtml(p.id)}"${p.id === cur ? " selected" : ""}>${escapeHtml(p.label)}</option>`
  ).join("");
  $("cfgKey").value = _llmCfg.apiKey || "";
  $("cfgQuick").value = _llmCfg.quickModel || "";
  $("cfgDeep").value = _llmCfg.deepModel || "";
  $("cfgBaseUrl").value = _llmCfg.baseUrl || "";
  syncProviderFields(cur);
  renderOwnerBox();
  updateConfigBadge();
}

// Seção de login do dono + visibilidade do BYOK conforme o estado do servidor.
function renderOwnerBox() {
  const box = $("ownerBox");
  if (!box) return;
  // Sem login configurado no servidor: ninguém vira dono → só BYOK, esconde a caixa.
  if (!_ownerLoginEnabled) { box.classList.add("hidden"); }
  else {
    box.classList.remove("hidden");
    $("ownerLoggedOut").classList.toggle("hidden", _isOwner);
    $("ownerLoggedIn").classList.toggle("hidden", !_isOwner);
  }
  // Dono logado: a chave do servidor já roda; a config própria vira OPCIONAL.
  const note = $("byokOptionalNote");
  if (_isOwner) {
    $("byokGrid").classList.add("byok-optional");
    if (note) note.classList.remove("hidden");
  } else {
    $("byokGrid").classList.remove("byok-optional");
    if (note) note.classList.add("hidden");
  }
}

// Mostra o campo Base URL só pros provedores que precisam (Ollama/self-host),
// atualiza placeholders de modelo com o padrão do provedor e a nota de fallback.
function syncProviderFields(provId) {
  const p = _providerMeta(provId);
  const needsBase = !!(p && p.needs_base_url);
  $("cfgBaseUrlField").classList.toggle("hidden", !needsBase);
  if (p) {
    $("cfgQuick").placeholder = p.default_quick || "(nome do modelo)";
    $("cfgDeep").placeholder = p.default_deep || "(nome do modelo)";
  }
  const st = $("cfgStatus");
  if (st && !st.dataset.sticky) {
    if (p && p.key_optional) st.textContent = "provedor local — chave opcional";
    else if (p && p.server_key) st.textContent = "servidor tem chave de fallback pra este provedor";
    else st.textContent = "";
  }
}

// Rótulo do botão da engrenagem: mostra se está com chave própria ou a do servidor.
function updateConfigBadge() {
  const lbl = $("configBtnLabel");
  const btn = $("configBtn");
  if (!lbl || !btn) return;
  btn.classList.remove("has-key", "is-owner");
  if (_isOwner) {
    lbl.textContent = "dono";                       // usa a chave do servidor
    btn.classList.add("is-owner");
  } else if (_llmCfg.apiKey) {
    const prov = _llmCfg.provider || (_llmMeta && _llmMeta.default_provider) || "";
    lbl.textContent = prov ? `chave: ${prov}` : "chave própria";
    btn.classList.add("has-key");
  } else {
    lbl.textContent = "Chaves";                     // público sem chave → precisa configurar
  }
  // Linha "ativo" dentro do painel.
  const act = $("cfgActive");
  if (act) {
    if (_isOwner && !_llmCfg.apiKey) {
      const m = _llmMeta ? ` · ${_llmMeta.default_provider} · ${_llmMeta.default_quick || ""}` : "";
      act.textContent = `Ativo: chave do servidor (dono)${m}`;
    } else if (_llmCfg.apiKey) {
      const prov = _llmCfg.provider || (_llmMeta && _llmMeta.default_provider) || "?";
      act.textContent = `Ativo: sua chave · ${prov}` +
        (_llmCfg.quickModel ? ` · ${_llmCfg.quickModel}` : "");
    } else {
      act.textContent = "Sem chave — informe a sua acima ou entre como dono para rodar.";
    }
  }
}

// Login do dono: envia a senha ao servidor (verificação server-side), que devolve
// um cookie de sessão HttpOnly. Recarrega o estado via /api/config.
async function ownerLogin() {
  const pass = $("ownerPass").value;
  const btn = $("ownerLoginBtn");
  const st = $("ownerStatus");
  btn.disabled = true;
  st.textContent = "entrando…"; st.className = "cfg-status";
  try {
    const res = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "same-origin", body: JSON.stringify({ password: pass }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      $("ownerPass").value = "";
      st.textContent = "";
      await applyConfig();          // recarrega owner/estado do servidor
      renderConfigPanel();
    } else {
      st.textContent = "❌ " + (data.error || "senha incorreta");
      st.className = "cfg-status err";
    }
  } catch (e) {
    st.textContent = "❌ erro de rede"; st.className = "cfg-status err";
  } finally {
    btn.disabled = false;
  }
}

async function ownerLogout() {
  try {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  } catch (e) { /* segue */ }
  await applyConfig();
  renderConfigPanel();
}

function _readConfigForm() {
  const provId = $("cfgProvider").value;
  const p = _providerMeta(provId);
  return {
    provider: provId,
    apiKey: $("cfgKey").value.trim(),
    quickModel: $("cfgQuick").value.trim(),
    deepModel: $("cfgDeep").value.trim(),
    baseUrl: (p && p.needs_base_url) ? $("cfgBaseUrl").value.trim() : "",
  };
}

function setCfgStatus(msg, kind) {
  const st = $("cfgStatus");
  if (!st) return;
  st.textContent = msg || "";
  st.className = "cfg-status" + (kind ? " " + kind : "");
  st.dataset.sticky = msg ? "1" : "";
}

function bindConfig() {
  // Comboboxes pesquisáveis dos modelos (rápido/pesado) — sobre os inputs do HTML.
  _modelCombos.cfgQuick = new ModelCombo("cfgQuick", "cfgQuickOpts");
  _modelCombos.cfgDeep = new ModelCombo("cfgDeep", "cfgDeepOpts");
  const toggle = () => {
    const panel = $("configPanel");
    panel.classList.toggle("hidden");
    // Ao ABRIR com uma chave já salva, popula os dropdowns na hora (lista vazia
    // num retorno é confuso). Guardado por _canListModels — não bate à toa.
    if (!panel.classList.contains("hidden") && !_modelItems.length) {
      refreshModels();
    }
  };
  $("configBtn").addEventListener("click", toggle);
  $("configClose").addEventListener("click", () => $("configPanel").classList.add("hidden"));
  // Trocar de provider invalida os modelos: limpa as listas e redispara a busca.
  $("cfgProvider").addEventListener("change", (e) => {
    setCfgStatus("");
    syncProviderFields(e.target.value);
    fillModelLists([]); $("cfgQuick").value = ""; $("cfgDeep").value = "";
    refreshModels();
  });
  // Ao DIGITAR/COLAR a chave: testa e puxa os modelos automaticamente (debounce).
  $("cfgKey").addEventListener("input", scheduleModels);
  $("cfgKey").addEventListener("paste", () => setTimeout(refreshModels, 0));
  $("cfgSave").addEventListener("click", () => {
    saveLlmCfg(_readConfigForm());
    setCfgStatus("salvo ✓", "ok");
    updateConfigBadge();
  });
  $("cfgClear").addEventListener("click", () => {
    saveLlmCfg({});
    renderConfigPanel();
    fillModelLists([]);
    setCfgStatus(_isOwner ? "usando a chave do servidor" : "chave própria limpa", "ok");
  });
  // "Testar chave" agora é reforço manual do mesmo fluxo (testa + lista).
  $("cfgTest").addEventListener("click", refreshModels);
  // "Testar modelo": pinga o rápido E o pesado escolhidos e mostra a latência.
  $("cfgTestModel").addEventListener("click", testModel);
  $("ownerLoginBtn").addEventListener("click", ownerLogin);
  $("ownerPass").addEventListener("keydown", (e) => { if (e.key === "Enter") ownerLogin(); });
  $("ownerLogoutBtn").addEventListener("click", ownerLogout);
}

// Erro de "precisa de chave" (403 need_key): abre a config e aponta o caminho.
function handleNeedKey(msg) {
  $("formError").textContent = msg || "Informe sua chave nas Configurações (⚙️).";
  $("configPanel").classList.remove("hidden");
  scrollToOpen($("configPanel"));
}

// ---- Combobox pesquisável de modelos (quick/deep) ---------------------------
// Com OpenRouter a chave dá acesso a ~400 modelos: um <select>/<datalist> sem
// busca é inutilizável (o Samyr não achou z-ai/glm-5.2). Aqui o campo vira um
// input que FILTRA a lista em tempo real, casando id E nome (ex.: "glm 5.2" acha
// z-ai/glm-5.2). Teclado ↑/↓/Enter/Esc, texto livre aceito (fallback), mobile ok.
const MODEL_LIST_MAX = 60;        // teto de opções exibidas (a lista é filtrada)
const _modelCombos = {};          // { cfgQuick: ModelCombo, cfgDeep: ModelCombo }
let _modelItems = [];             // última lista carregada (infos), reidrata combos

// Match token-a-token, case-insensitive, por substring em id + nome. Cada palavra
// digitada precisa aparecer em algum lugar (id ou nome) — não precisa ser prefixo
// nem estar em ordem. "glm 5.2" → z-ai/glm-5.2; "deepseek flash" → deepseek/…-flash.
function filterModels(items, query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return items;
  const toks = q.split(/\s+/).filter(Boolean);
  return items.filter((it) => {
    const hay = (it.id + " " + (it.name || "")).toLowerCase();
    return toks.every((t) => hay.includes(t));
  });
}

// Rótulo curto de preço (USD por 1M tokens) quando o provider manda — ajuda a
// escolher sem sair da tela. Sem preço → string vazia (só o id/nome aparecem).
function _priceLabel(it) {
  const fmt = (v) => (v == null ? null : (v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(3)}`));
  const i = fmt(it.price_in), o = fmt(it.price_out);
  if (i && o) return `${i}/${o} ·1M`;
  if (i) return `${i} in ·1M`;
  return "";
}

class ModelCombo {
  constructor(inputId, listId) {
    this.input = $(inputId);
    this.list = $(listId);
    this.items = [];        // [{id, name, price_in, price_out}]
    this.view = [];         // itens filtrados exibidos agora
    this.active = -1;       // índice destacado (navegação por teclado)
    this.open = false;
    this._blurTimer = null;
    this._bind();
  }

  setItems(items) {
    this.items = Array.isArray(items) ? items : [];
    if (this.open) this._filter(this.input.value);   // reidrata se já está aberto
  }

  _bind() {
    this.input.addEventListener("input", () => this._openWith(this.input.value));
    this.input.addEventListener("focus", () => this._openWith(this.input.value));
    this.input.addEventListener("keydown", (e) => this._onKey(e));
    // Fecha ao sair, mas com atraso pra o mousedown na opção rodar antes.
    this.input.addEventListener("blur", () => {
      this._blurTimer = setTimeout(() => this._close(), 150);
    });
    // mousedown (não click) dispara ANTES do blur fechar a lista.
    this.list.addEventListener("mousedown", (e) => {
      const li = e.target.closest("[data-val]");
      if (!li) return;
      e.preventDefault();                 // não rouba o foco do input
      this._choose(li.getAttribute("data-val"));
    });
  }

  _openWith(q) {
    if (!this.items.length) { this._close(); return; }   // nada carregado ainda
    this._filter(q);
    this._show();
  }

  _filter(q) {
    this.view = filterModels(this.items, q).slice(0, MODEL_LIST_MAX);
    this.active = -1;
    this._render();
  }

  _render() {
    if (!this.view.length) {
      this.list.innerHTML =
        `<li class="combo-empty" aria-disabled="true">nenhum modelo casa — Enter usa o texto digitado</li>`;
      return;
    }
    this.list.innerHTML = this.view.map((it, i) => {
      const price = _priceLabel(it);
      const name = (it.name && it.name !== it.id)
        ? `<span class="combo-name">${escapeHtml(it.name)}</span>` : "";
      const priceEl = price ? `<span class="combo-price">${escapeHtml(price)}</span>` : "";
      return `<li class="combo-opt${i === this.active ? " is-active" : ""}" role="option"` +
             ` data-val="${escapeHtml(it.id)}" id="${this.list.id}-o${i}"` +
             ` aria-selected="${i === this.active}">` +
             `<span class="combo-id">${escapeHtml(it.id)}</span>${name}${priceEl}</li>`;
    }).join("");
  }

  _show() {
    this.open = true;
    this.list.classList.remove("hidden");
    this.input.setAttribute("aria-expanded", "true");
  }

  _close() {
    this.open = false;
    this.active = -1;
    this.list.classList.add("hidden");
    this.input.setAttribute("aria-expanded", "false");
    this.input.removeAttribute("aria-activedescendant");
  }

  _move(delta) {
    if (!this.open) this._openWith(this.input.value);
    if (!this.view.length) return;
    this.active = (this.active + delta + this.view.length) % this.view.length;
    this._render();
    const el = this.list.children[this.active];
    if (el) {
      el.scrollIntoView({ block: "nearest" });
      this.input.setAttribute("aria-activedescendant", el.id);
    }
  }

  _onKey(e) {
    if (e.key === "ArrowDown") { e.preventDefault(); this._move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); this._move(-1); }
    else if (e.key === "Enter") {
      if (this.open && this.active >= 0 && this.view[this.active]) {
        e.preventDefault();
        this._choose(this.view[this.active].id);
      } else {
        this._close();                    // texto livre: aceita o que está digitado
      }
    } else if (e.key === "Escape") {
      if (this.open) { e.preventDefault(); e.stopPropagation(); this._close(); }
    }
  }

  _choose(val) {
    this.input.value = val;
    clearTimeout(this._blurTimer);
    this._close();
    this.input.focus();
  }
}

// Alimenta os dois comboboxes (rápido/pesado) com os modelos reais da chave.
// Aceita objetos {id,name,price_*} (endpoint atual) ou ids soltos (compat).
function fillModelLists(models) {
  _modelItems = (models || [])
    .map((m) => (typeof m === "string"
      ? { id: m, name: m, price_in: null, price_out: null } : m))
    .filter((m) => m && m.id);
  for (const k of Object.keys(_modelCombos)) _modelCombos[k].setItems(_modelItems);
}

// Pré-seleciona defaults sensatos do provider quando o campo está vazio e o modelo
// existe na lista (senão deixa o usuário escolher/digitar).
function preselectDefaults() {
  const p = _providerMeta($("cfgProvider").value);
  if (!p) return;
  const ids = new Set(_modelItems.map((m) => m.id));
  if (!$("cfgQuick").value && p.default_quick && ids.has(p.default_quick)) $("cfgQuick").value = p.default_quick;
  if (!$("cfgDeep").value && p.default_deep && ids.has(p.default_deep)) $("cfgDeep").value = p.default_deep;
}

// Provider dá pra listar agora? (evita bater no backend sem chave onde ela é obrigatória)
function _canListModels(form) {
  if (_isOwner) return true;                       // dono usa a env do servidor
  if (form.provider === "openrouter") return true; // catálogo público
  if (form.provider === "ollama") return !!form.baseUrl;
  return (form.apiKey || "").length >= 8;          // demais: precisa da chave
}

let _modelsTimer = null;
let _modelsAbort = null;
let _modelsSeq = 0;

// Testa a chave E puxa a lista de modelos numa tacada (POST /api/models). Cancela a
// requisição anterior se a chave/provider mudar de novo. Sucesso ✅ N modelos +
// popula os dropdowns; falha ❌ mensagem humana (041) e cai no texto livre.
async function refreshModels() {
  const form = _readConfigForm();
  if (!_canListModels(form)) { setCfgStatus("", ""); return; }
  const seq = ++_modelsSeq;
  if (_modelsAbort) _modelsAbort.abort();
  _modelsAbort = new AbortController();
  setCfgStatus("testando chave e carregando modelos…", "");
  const headers = { "Content-Type": "application/json" };
  if (form.apiKey) headers["X-LLM-Key"] = form.apiKey;
  const body = { llm_provider: form.provider };
  if (form.baseUrl) body.backend_url = form.baseUrl;
  try {
    const res = await fetch("/api/models", {
      method: "POST", headers, credentials: "same-origin",
      body: JSON.stringify(body), signal: _modelsAbort.signal,
    });
    const data = await res.json();
    if (seq !== _modelsSeq) return;   // resposta velha: a chave já mudou
    if (data.ok) {
      fillModelLists(data.models);
      preselectDefaults();
      setCfgStatus(`✅ chave válida — ${data.count} modelos carregados`, "ok");
    } else {
      fillModelLists([]);
      setCfgStatus(`❌ ${data.error || "não deu pra listar os modelos"}`, "err");
    }
  } catch (e) {
    if (e.name === "AbortError" || seq !== _modelsSeq) return;
    setCfgStatus("❌ erro de rede ao listar modelos", "err");
  }
}

// Debounce pra não disparar a cada tecla ao digitar/colar a chave.
function scheduleModels() {
  clearTimeout(_modelsTimer);
  _modelsTimer = setTimeout(refreshModels, 500);
}

// Latência legível: <1s em ms, senão em s com uma casa (vírgula pt-BR).
function fmtLatency(ms) {
  if (ms == null) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1).replace(".", ",")} s` : `${ms} ms`;
}

// "Testar modelo": pinga o modelo RÁPIDO e o PESADO escolhidos com um prompt trivial
// (POST /api/test-model) e mostra ✅ latência de cada (ou ❌ mensagem humana), SEM
// rodar a análise. A chave viaja só no header X-LLM-Key; nada dela aparece na tela.
let _modelTestAbort = null;
async function testModel() {
  const form = _readConfigForm();
  // Mesmo gate do analyze: sem login do dono e sem chave própria não há o que testar.
  if (!_isOwner && !(form.apiKey || "").length) {
    renderModelTest({ error: "Informe sua chave nas Configurações (⚙️) antes de testar o modelo." });
    return;
  }
  const btn = $("cfgTestModel");
  if (_modelTestAbort) _modelTestAbort.abort();
  _modelTestAbort = new AbortController();
  if (btn) btn.disabled = true;
  const box = $("cfgModelTest");
  if (box) { box.classList.remove("hidden"); box.innerHTML = '<div class="mt-row">pingando o modelo rápido e o pesado…</div>'; }
  const headers = { "Content-Type": "application/json" };
  if (form.apiKey) headers["X-LLM-Key"] = form.apiKey;
  const body = { llm_provider: form.provider };
  if (form.quickModel) body.quick_think_llm = form.quickModel;
  if (form.deepModel) body.deep_think_llm = form.deepModel;
  if (form.baseUrl) body.backend_url = form.baseUrl;
  try {
    const res = await fetch("/api/test-model", {
      method: "POST", headers, credentials: "same-origin",
      body: JSON.stringify(body), signal: _modelTestAbort.signal,
    });
    const data = await res.json();
    renderModelTest(data);
  } catch (e) {
    if (e.name === "AbortError") return;
    renderModelTest({ error: "erro de rede ao testar o modelo" });
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Desenha o resultado do teste: uma linha por modelo (⚡ rápido / 🧠 pesado) com
// ✅ latência + trecho, ou ❌ mensagem humana. `sample`/`error` vêm do modelo →
// sempre escapados (anti-XSS). Erro de topo (need_key/rede) vira uma linha só.
function renderModelTest(data) {
  const box = $("cfgModelTest");
  if (!box) return;
  box.classList.remove("hidden");
  const models = (data && data.models) || [];
  if ((!models.length) && data && data.error) {
    box.innerHTML = `<div class="mt-row err">❌ ${escapeHtml(String(data.error))}</div>`;
    return;
  }
  if (!models.length) {
    box.innerHTML = '<div class="mt-row err">❌ não deu pra testar o modelo</div>';
    return;
  }
  box.innerHTML = models.map((m) => {
    const icon = m.role === "deep" ? "🧠" : "⚡";
    const label = escapeHtml(String(m.label || m.role || ""));
    const name = escapeHtml(String(m.model || "(padrão do provedor)"));
    if (m.ok) {
      const sample = m.sample ? ` — “${escapeHtml(String(m.sample))}”` : "";
      return `<div class="mt-row ok">${icon} ${label} <code>${name}</code>: `
        + `✅ <b>${escapeHtml(fmtLatency(m.latency_ms))}</b>${sample}</div>`;
    }
    return `<div class="mt-row err">${icon} ${label} <code>${name}</code>: `
      + `❌ ${escapeHtml(String(m.error || "falhou"))}</div>`;
  }).join("");
}

function init() {
  loadLlmCfg();
  applyConfig();
  bindConfig();
  $("analyzeForm").addEventListener("submit", startAnalysis);
  $("ticker").addEventListener("input", () => {
    const t = $("ticker").value.trim().toUpperCase();
    $("assetHint").innerHTML = /-(USD|USDT)$|^BTC|^ETH/.test(t) ? `Detectado: cripto — inclui taxa de financiamento <span class="orig">(funding)</span>, contratos em aberto <span class="orig">(open interest)</span> e liquidações <span class="orig">(liquidations)</span>.` : "";
    scheduleTickerSuggest($("ticker").value.trim());
  });
  $("netNote").textContent = "acesse por " + location.host;
  bindHistoryTabs();
  bindReeval();
  bindConfront();
  bindReanalyzeBar();
  bindExportPdf();
  loadHistory();
  // Ao abrir: se havia um run vivo sendo acompanhado, reengata o progresso; senão,
  // mostra a análise mais recente. Sem isso o refresh no meio de um run "esquecia".
  resumeActiveRun().then((resumed) => { if (!resumed) openLatestRun(); });
  // Voltar o app pro primeiro plano (aba de fundo afrouxa/pausa o setInterval, muito
  // no mobile): força um poll imediato + reinicia o intervalo; se o timer morreu,
  // reengata o run persistido. Contorna o throttle e retoma o progresso na hora.
  document.addEventListener("visibilitychange", onVisibleForeground);
  startHistoryAutoRefresh();
  startPriceAutoRefresh();
}

function onVisibleForeground() {
  if (document.visibilityState !== "visible") return;
  if (_watchedRunId && pollTimer) watchRun(_watchedRunId);   // poll já + intervalo novo
  else resumeActiveRun();                                    // timer morreu: reengata o vivo
  refreshPrices();                                           // preços live ao voltar pra aba
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
