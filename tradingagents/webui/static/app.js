"use strict";

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let TZ_LABEL = "GMT-4 (Manaus)";

// Segundo plano: cada análise roda numa thread própria no servidor e continua
// mesmo se o usuário troca de ativo, sai da tela ou recarrega. Estes controlam
// só a VISÃO — qual run está sendo acompanhado ao vivo, quais estavam rodando na
// última atualização da lista, e quais terminaram sozinhos (ganham "pronto").
let _watchedRunId = "";              // run cujo progresso está na tela agora
let _refreshBusy = "";               // etapa cujo "atualizar" (task 002) está em voo
let _refreshSeen = false;            // o servidor já confirmou o refresh no snapshot
// Cancelamento PENDENTE (task 013): o cancel é cooperativo — a run só encerra no
// próximo limite de nó/LLM. Enquanto o pedido está pendente E a run segue 'running',
// o botão fica TRAVADO em 'parando…' e o poll NÃO o reabre (senão o usuário acha que
// o clique não pegou e clica de novo). Guarda o run_id pedido; some quando vira terminal.
let _cancelPending = "";
let _cancelPause = false;             // o pedido foi Pausar (true) ou Parar (false)?
const _STOP_LABEL = "Parar análise";
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
// Token de CONTROLE da run (Parar/Pausar): o servidor devolve junto do run_id em
// /api/analyze e só quem INICIOU a análise o tem — o run_id sozinho não vale como
// prova, ele aparece na lista pública de /api/runs. Guardado por run (últimos 10) e
// devolvido no header X-Run-Token; sobrevive a refresh pra o Parar seguir funcionando
// depois do reengate. O dono logado não precisa dele (a sessão já o identifica).
const _RUN_TOKEN_KEY = "td_run_tokens";
function rememberRunToken(runId, token) {
  if (!runId || !token) return;
  try {
    const all = JSON.parse(localStorage.getItem(_RUN_TOKEN_KEY) || "{}") || {};
    all[runId] = token;
    const ids = Object.keys(all);
    // poda simples: o mapa não pode crescer pra sempre no navegador
    if (ids.length > 10) ids.slice(0, ids.length - 10).forEach((k) => delete all[k]);
    localStorage.setItem(_RUN_TOKEN_KEY, JSON.stringify(all));
  } catch (e) { /* quota / modo privado: perde só o Parar, não quebra a análise */ }
}
function runTokenHeader(runId) {
  try {
    const all = JSON.parse(localStorage.getItem(_RUN_TOKEN_KEY) || "{}") || {};
    return all[runId] ? { "X-Run-Token": all[runId] } : {};
  } catch (e) { return {}; }
}
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
  // Provedor por NÍVEL (task 027 → primário na 017): cada nível manda o SEU provedor e,
  // quando é um self-host (Ollama/compatível), o SEU endpoint — senão o endpoint de um
  // nível ia parar no client do outro. Os modelos reusam deep_think_llm/quick_think_llm.
  if (c.advanced) {
    body.advanced = true;
    if (c.quickProvider) body.quick_provider = c.quickProvider;
    if (c.deepProvider) body.deep_provider = c.deepProvider;
    if (c.quickBaseUrl) body.quick_backend_url = c.quickBaseUrl;
    if (c.deepBaseUrl) body.deep_backend_url = c.deepBaseUrl;
  }
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

// Setup state emitted by the deterministic actionable plan -> rótulo pt-BR.
//
// Era um par [emoji, rótulo]. O emoji saiu (DA-076) e o estado NÃO ficou sem
// marcador: quem o carrega é a COR da classe que acompanha o rótulo em toda
// superfície onde ele aparece — `.sc-state.ativo` (verde), `.sc-state.aguardar_*`
// (âmbar), `.sc-state.sem_*` (apagado) no card, e a mesma família de classes no
// chip da lateral. Cor + palavra, nunca o vazio.
const SETUP_PT = {
  // "Setup ativo agora" saiu (DA-121). Era o ÚNICO rótulo do produto cuja leitura
  // natural em português ("está ativo", "está rodando") apontava para a fase
  // ERRADA — o Samyr leu como "em movimento para o alvo", que é o oposto: `ativo`
  // quer dizer que o preço está TOCANDO a entrada agora. "Na entrada" não tem como
  // ser lida como "já andou".
  ativo: "Na entrada agora",
  aguardar_pullback: "Aguardar recuo à média",
  aguardar_rompimento: "Aguardar rompimento",
  sem_setup: "Sem setup de preço definido",
  sem_dado: "Sem dado suficiente",
  intradiario_indisponivel: "Intradiário indisponível",
};

// ============ A FASE — UM eixo temporal para a tela inteira (DA-121) ==========
//
// ESPELHO de `webui/fases.py`, que é a AUTORIDADE. Um teste solda os dois: sem
// ele a próxima palavra nova nasce de um lado só, que é exatamente como as três
// taxonomias (lateral, scan, sinais) apareceram sem tradução entre elas.
//
// As taxonomias NÃO se fundem — cada uma descreve um sujeito diferente (o plano
// da run, a leitura de um frame, a oportunidade agregada). O que passa a ser
// único é o EIXO: quatro fases, quatro palavras, as mesmas em toda superfície. O
// MECANISMO ("recuo à média", "rompimento do ponto 2") vira qualificador ao lado,
// nunca sinônimo.
const FASE_PT = {
  agora: "NA ENTRADA",
  esperando: "AGUARDANDO",
  andou: "JÁ ANDOU",
  morreu: "INVALIDADO",
  sem_leitura: "SEM LEITURA",
};
const FASE_AJUDA = {
  agora: "o preço está no ponto de entrar — é a hora de agir",
  esperando: "o gatilho ainda não veio",
  andou: "acionou e o preço já passou da entrada",
  morreu: "a premissa rompeu — não há trade",
  sem_leitura: "não há leitura para este ativo neste frame",
};
const FASE_DO_SETUP_STATE = {
  ativo: "agora",
  aguardar_pullback: "esperando",
  aguardar_rompimento: "esperando",
  sem_setup: "sem_leitura",
  sem_dado: "sem_leitura",
  intradiario_indisponivel: "sem_leitura",
};
const FASE_DO_SCAN_ESTADO = {
  em_gatilho: "agora",
  formando: "esperando",
  em_movimento: "andou",
  invalidou: "morreu",
  sem_setup: "sem_leitura",
  sem_dado: "sem_leitura",
  vetado: "sem_leitura",
  zona_neutra: "agora",
};
const FASE_DA_OPORTUNIDADE = {
  entrada: "agora", a_caminho: "esperando", passou: "andou", conflito: null,
};
const MECANISMO_PT = {
  aguardar_pullback: "recuo à média",
  aguardar_rompimento: "rompimento do ponto 2",
  formando: "padrão formando",
  ativo: "recuo à média",
  em_gatilho: "gatilho rompido",
  em_movimento: "gatilho ficou para trás",
  invalidou: "premissa rompida",
};
function faseDoSetupState(st) { return FASE_DO_SETUP_STATE[st] || null; }
function faseDoScanEstado(e) { return FASE_DO_SCAN_ESTADO[e] || null; }
function faseRotulo(f) { return f ? (FASE_PT[f] || "") : ""; }
function faseAjuda(f) { return f ? (FASE_AJUDA[f] || "") : ""; }
function mecanismoPt(e) { return MECANISMO_PT[e] || ""; }

// DE QUAL setup veio o estado acima. São dois independentes, desenhados na mesma
// tela, que podem coexistir e discordar: o RECUO À MÉDIA (faixa verde) e o 1-2-3
// (gatilho no ponto 2). "Setup ativo agora" sozinho não dizia de quem falava — e
// no ZEC-USD 4h falava da média enquanto o 1-2-3 acionado roubava o crédito.
const SETUP_SOURCE_PT = {
  recuo_media: "recuo à média",
  123: "padrão 1-2-3",
};
function setupSourcePt(src) {
  return SETUP_SOURCE_PT[src] || SETUP_SOURCE_PT[String(src)] || "";
}

// Rótulo COMPACTO do setup state pra chip estreito da watchlist (task 010): o
// veredito de uma run 1-2-3 é o estado do setup, não "CONCLUÍDO". SETUP_PT tem
// a frase completa (cabeçalho da análise); aqui a forma curta cabe na coluna.
const SETUP_COMPACT = {
  // "Ativo" era a palavra que induzia ao erro (DA-121) — e no chip estreito, onde
  // não cabe explicação, era ainda mais sozinha. Vai a fase por extenso.
  ativo: "Na entrada",
  aguardar_pullback: "Aguardar recuo",
  aguardar_rompimento: "Aguardar rompimento",
  sem_setup: "Sem setup",
  sem_dado: "Sem dado",
  intradiario_indisponivel: "Indisponível",
};

// 1-2-3 direction/state -> pt-BR. Compra (fundo ascendente) e venda (topo
// descendente) recebem COR distinta no card e no gráfico (fork brief 24/08) — é ela
// que carrega a direção desde que o emoji saiu (DA-076): `.sc-123` azul de compra,
// `.sc-123.sc-venda` laranja, as mesmas de `PAT_COLORS` no canvas.
// O índice 0 fica VAZIO de propósito: o rótulo é o [1] em todo consumidor, e
// renumerar espalharia a mudança por lugares que não têm nada a ver com emoji.
const PAT_DIR = {
  compra: ["", "de compra", "fundo ascendente"],
  venda: ["", "de venda", "topo descendente"],
};
// rótulo pt-BR do estado do gatilho 1-2-3 (task 014) — "rompeu_retracou" é token
// de máquina; aqui vira texto legível pra nota do gráfico não mostrar o snake_case.
const PAT_STATE = {
  acionado: "acionado",
  formando: "em formação",
  rompeu_retracou: "rompeu e retraçou (não confirmado)",
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

// Um MOMENTO da tira do cabeçalho: "29/08" quando o dado só tem data, "29/08 20:00"
// quando ele carrega a HORA — que é o caso de um frame intradiário, onde o preço da
// análise é o de um candle específico. Aceita "2026-08-29", "2026-08-29 20:00" e
// "2026-08-29T20:00" (as três formas que o backend devolve). Não inventa hora: sem
// hora no dado, nada aparece.
function fmtMomento(iso) {
  const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
  if (!m) return escapeHtml(String(iso || ""));
  return `${m[3]}/${m[2]}` + (m[4] ? ` ${m[4]}:${m[5]}` : "");
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

// Ao abrir um resultado/comparação, rola pro painel aberto. O controle de método/TF
// agora vive na barra ÚNICA fixa no topo (launcher), sempre à mão — não precisa mais
// rolar até ele.
function scrollToOpen(panel) {
  if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

// PARAR/PAUSAR (task 026): mostra/esconde os controles conforme o snapshot e liga os
// cliques 1x. Parar aparece sempre que a run é cancelável; Pausar só quando retomável
// (run de dono/servidor + checkpoint — BYOK não é retomável, some sem prometer nada).
let _runCtlBound = false;
function updateRunControls(snap) {
  const ctl = $("progressCtl");
  if (!ctl) return;
  const alive = snap.status === "running" && snap.cancellable !== false;
  ctl.classList.toggle("hidden", !alive);
  const pauseBtn = $("pauseRunBtn");
  if (pauseBtn) pauseBtn.classList.toggle("hidden", !(alive && snap.resumable));
  const stopBtn = $("stopRunBtn");
  // Cancel pendente DESTA run e ela ainda viva: mantém 'parando…' travado — não
  // reabre (task 013). Senão libera normalmente enquanto a run está viva.
  const pending = _cancelPending && _cancelPending === (snap.run_id || _watchedRunId) && alive;
  if (pending) {
    applyCancelPendingUI();
  } else if (alive) {
    if (stopBtn) stopBtn.disabled = false;
    if (pauseBtn) pauseBtn.disabled = false;
  }
  if (!_runCtlBound) {
    _runCtlBound = true;
    if (stopBtn) stopBtn.addEventListener("click", () => stopRun(false));
    if (pauseBtn) pauseBtn.addEventListener("click", () => stopRun(true));
  }
}

// Trava visual do 'parando…' enquanto o cancelamento está pendente (task 013): botão
// desabilitado, rótulo claro no botão E no progresso, classe .is-stopping (pulso) pra
// mostrar que registrou. Idempotente — o poll a re-aplica a cada 2s sem piscar.
function applyCancelPendingUI() {
  const stopBtn = $("stopRunBtn"), pauseBtn = $("pauseRunBtn");
  if (stopBtn) {
    stopBtn.disabled = true;
    stopBtn.classList.add("is-stopping");
    stopBtn.textContent = _cancelPause ? "pausando…" : "parando…";
  }
  if (pauseBtn) pauseBtn.disabled = true;
  const lbl = $("progressLabel");
  if (lbl) lbl.textContent = _cancelPause
    ? "pausando — aguarde o passo atual encerrar…"
    : "interrompendo — aguarde o passo atual encerrar…";
}

// Solta a trava do 'parando…' (cancel falhou, terminou, ou troca de run): restaura o
// rótulo/estado do botão pra reuso limpo.
function clearCancelPending() {
  _cancelPending = "";
  const stopBtn = $("stopRunBtn");
  if (stopBtn) { stopBtn.classList.remove("is-stopping"); stopBtn.textContent = _STOP_LABEL; }
}

// Parar (pause=false) ou Pausar (true) a run em andamento. Cooperativo: o servidor
// sinaliza o worker e a run encerra em poucos segundos; o poll seguinte vê 'cancelled'
// e libera a UI. Manda a chave própria (BYOK) no header, igual às outras chamadas.
async function stopRun(pause) {
  const runId = _watchedRunId;
  if (!runId) return;
  if (_cancelPending === runId) return;            // já pedido — 1 clique basta (013)
  // Trava o estado 'parando…' JÁ, antes do await: o poll seguinte não reabre o botão.
  _cancelPending = runId;
  _cancelPause = !!pause;
  applyCancelPendingUI();
  try {
    const { headers } = llmRequestParts();
    const res = await fetch("/api/run/" + encodeURIComponent(runId) + "/cancel", {
      method: "POST", credentials: "same-origin",
      // X-Run-Token: prova de que ESTA run é minha (o servidor não aceita mais um
      // run_id sozinho — era assim que dava pra parar a análise dos outros).
      headers: { ...headers, ...runTokenHeader(runId), "Content-Type": "application/json" },
      body: JSON.stringify({ pause: !!pause }),
    });
    // Só MANTÉM 'parando…' se o servidor confirmou (200). Não-200 = não registrou →
    // destrava e avisa, pra o usuário poder tentar de novo (013).
    if (!res.ok) throw new Error("cancel HTTP " + res.status);
  } catch (e) {
    clearCancelPending();
    const stopBtn = $("stopRunBtn"), pauseBtn = $("pauseRunBtn");
    if (stopBtn) stopBtn.disabled = false;
    if (pauseBtn) pauseBtn.disabled = false;
    $("formError").textContent = "não consegui " + (pause ? "pausar" : "parar") + " — tente de novo";
  }
}

// Run interrompida pelo usuário (task 026): estado honesto, não é erro nem resultado.
// Libera a UI (esconde progresso, reabilita o launcher). PARAR → aviso curto; PAUSAR →
// oferece Retomar (continua do checkpoint da 022).
function renderCancelled(snap) {
  clearInterval(pollTimer); pollTimer = null;
  clearActiveRun();
  clearCancelPending();          // cancel efetivado: solta a trava do 'parando…' (013)
  $("progressPanel").classList.add("hidden");
  const ctl = $("progressCtl"); if (ctl) ctl.classList.add("hidden");
  $("runBtn").disabled = false;
  const rid = snap.run_id || _watchedRunId;
  const resumeBar = $("resumeBar");
  if (snap.paused && rid) {
    $("formError").textContent = "";
    if (resumeBar) {
      $("resumeMsg").textContent = "Análise pausada — retome do último estágio (reaproveita o que já rodou).";
      resumeBar.dataset.runId = rid;
      resumeBar.classList.remove("hidden");
    }
  } else {
    if (resumeBar) resumeBar.classList.add("hidden");
    $("formError").textContent = "Análise interrompida pelo usuário.";
  }
}

// Retomar a run pausada (task 026): POST resume; o servidor continua do checkpoint e
// volta a rodar. Reengata o progresso ao vivo.
async function resumeRun() {
  const bar = $("resumeBar");
  const runId = bar ? bar.dataset.runId : "";
  if (!runId) return;
  const btn = $("resumeRunBtn");
  if (btn) btn.disabled = true;
  try {
    const { headers } = llmRequestParts();
    const res = await fetch("/api/run/" + encodeURIComponent(runId) + "/resume", {
      method: "POST", credentials: "same-origin",
      headers: { ...headers, "Content-Type": "application/json" }, body: "{}",
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.run_id) {
      if (bar) bar.classList.add("hidden");
      watchRun(data.run_id);   // volta a acompanhar o progresso ao vivo
    } else {
      $("formError").textContent = (data && data.error) || "não deu pra retomar";
    }
  } catch (e) {
    $("formError").textContent = "erro de rede ao retomar";
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderProgress(snap) {
  $("progressPanel").classList.remove("hidden");
  // Controles PARAR/PAUSAR (task 026): visíveis enquanto a run está viva.
  updateRunControls(snap);
  // A barra ÚNICA (launcher) é persistente — fica no topo durante a run, não some.
  const tk = $("progressTicker");
  if (tk) {
    // qual ativo está sendo analisado — some quando não sabemos o ticker (start
    // sintético antes do 1º poll já manda o ticker, então quase sempre aparece)
    tk.textContent = snap.ticker || "";
    tk.classList.toggle("hidden", !snap.ticker);
  }
  const p = snap.progress || {};
  $("progressPhase").textContent = p.phase || "…";
  // Cancel pendente: NÃO deixa o p.label do poll sobrescrever o 'interrompendo…' —
  // updateRunControls (acima) já pôs a mensagem certa (task 013).
  const pendingHere = _cancelPending && _cancelPending === (snap.run_id || _watchedRunId);
  // Atualizar etapa em voo (task 002): pausar → rebobinar → re-entrar é UM gesto pro
  // usuário. Enquanto o servidor confirma o refresh, a tela diz o que está havendo em
  // vez de piscar "pausada" e sumir com o progresso.
  if (snap.refreshing) {
    _refreshSeen = true;
    $("progressPhase").textContent = "Atualizando";
    $("progressLabel").textContent =
      "atualizando “" + (snap.refreshing.label || "etapa") + "” com dados frescos…";
  } else {
    if (_refreshSeen) { _refreshSeen = false; _refreshBusy = ""; }
    if (!pendingHere) $("progressLabel").textContent = p.label || "";
  }
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
      const stateTxt = { pending: "aguardando", running: "rodando…", done: "concluída", reused: "reusada do cache" }[st] || "";
      return `<li class="cmp-step is-${st}">` +
        `<span class="cmp-step-n">${i + 1}</span>` +
        `<span class="cmp-step-body"><span class="cmp-step-label">${escapeHtml(s.label)}</span>` +
        `<span class="cmp-step-state">${stateTxt}</span></span></li>`;
    }).join("");
    return;
  }

  // Análise única: chips das etapas.
  cmpStepsEl.classList.add("hidden");
  steps.classList.remove("hidden");
  renderSteps(steps, p, snap);
}

// Chips das etapas. O ESTADO vem do motor (progress.steps[].state), não de um
// cruzamento plan×reached no front: só o motor distingue a etapa que ESTE run
// executou da que voltou PRONTA do checkpoint numa retomada — e essa precisa
// aparecer verde, senão a tela pinta de cinza justamente o trabalho preservado
// (task 002). Snapshot antigo/sem `steps` cai no cruzamento de antes, intacto.
// Cada etapa concluída ganha o ↻ "atualizar": re-roda SÓ ela com dado fresco.
function renderSteps(steps, p, snap) {
  const activeLabel = p.label;
  const reached = new Set((p.reached || []).map((r) => r.label));
  const list = (p.steps && p.steps.length) ? p.steps : (p.plan || []).map((s) => ({
    label: s.label, node: s.node || "",
    state: reached.has(s.label) ? (s.label === activeLabel ? "running" : "done") : "pending",
  }));
  if (!list.length) return;
  const canRefresh = !!(_isOwner && snap.resumable && (snap.run_id || _watchedRunId));
  // Assinatura do que a lista DESENHA: sem isto o innerHTML seria refeito a cada
  // poll de 2s e o botão piscaria/perderia o clique. Só re-renderiza no que mudou.
  const sig = list.map((s) => (s.node || s.label) + ":" + s.state).join("|") +
    "|" + (canRefresh ? "1" : "0") + "|" + (snap.status || "") + "|" + _refreshBusy;
  if (steps.dataset.sig === sig) return;
  steps.dataset.sig = sig;
  steps.innerHTML = list.map((s) => {
    const st = s.state || "pending";
    const done = st === "done" || st === "reused";
    const cls = [done ? "done" : "", st === "reused" ? "reused" : "",
                 (st === "running" && snap.status === "running") ? "active" : ""]
      .filter(Boolean).join(" ");
    const short = escapeHtml(String(s.label || "").split(" — ")[0]);
    // "cache" = veio pronta do checkpoint, custo zero (DA-058: reúso é dito, não fingido).
    const mark = st === "reused"
      ? '<span class="step-reused" title="reaproveitada de onde a análise parou — custo zero">cache</span>'
      : "";
    const busy = _refreshBusy && _refreshBusy === s.node;
    const btn = (canRefresh && done && s.node)
      ? `<button type="button" class="step-refresh${busy ? " is-busy" : ""}"` +
        ` data-node="${escapeHtml(s.node)}"${busy ? " disabled" : ""}` +
        ` title="Atualizar esta etapa com dados frescos — re-roda só ela"` +
        ` aria-label="Atualizar ${short} com dados frescos">↻</button>`
      : "";
    return `<li class="${cls}" data-label="${escapeHtml(s.label)}"` +
      ` data-state="${escapeHtml(st)}"><span class="step-name">${short}</span>` +
      `${mark}${btn}</li>`;
  }).join("");
  bindStepRefresh(steps);
}

let _stepsBound = false;
function bindStepRefresh(steps) {
  if (_stepsBound) return;
  _stepsBound = true;
  steps.addEventListener("click", (ev) => {
    const btn = ev.target.closest && ev.target.closest(".step-refresh");
    if (btn) refreshStep(btn.getAttribute("data-node"), btn);
  });
}

// POST /api/run/<id>/refresh-step: re-roda SÓ aquela etapa com DADO FRESCO,
// reaproveitando as anteriores do checkpoint. Não é o "Escalar etapa" (027, que
// troca o LLM) — aqui o modelo é o mesmo e o que muda é o número. Owner-gated no
// servidor, que recusa run não-resumível (BYOK) com mensagem honesta.
async function refreshStep(node, btn) {
  const runId = _watchedRunId;
  if (!runId || !node || _refreshBusy) return;
  _refreshBusy = node;
  if (btn) { btn.disabled = true; btn.classList.add("is-busy"); }
  const fail = (msg) => {
    _refreshBusy = ""; _refreshSeen = false;
    $("formError").textContent = msg;
    if (btn) { btn.disabled = false; btn.classList.remove("is-busy"); }
  };
  try {
    const { headers } = llmRequestParts();
    const res = await fetch("/api/run/" + encodeURIComponent(runId) + "/refresh-step", {
      method: "POST", credentials: "same-origin",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ node }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      fail((data && data.error) || "não deu pra atualizar essa etapa");
      return;
    }
    $("formError").textContent = "";
    // o servidor pausa → rebobina → re-entra; se o poll tinha parado (run pausada
    // ou terminada na tela), reengata pra acompanhar a etapa voltando a rodar.
    if (!pollTimer) watchRun(runId);
  } catch (e) {
    fail("erro de rede ao atualizar a etapa");
  }
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
      // rótulo do agente + selo de TIMEFRAME(s) da etapa (task 009) + selo do LLM que
      // rodou esta etapa (atribuição, task 024). Os dois selos vivem no mesmo lugar.
      sum.innerHTML = `<span class="tk-label">${escapeHtml(it.label)}</span>` +
        `<span class="tk-tf" data-tk-tf></span>` +
        `<span class="tk-model" data-tk-model></span>` +
        `<span class="tk-reused" data-tk-reused></span>`;
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
    // Timeframe(s) que a etapa analisou (task 009): selo ao lado do modelo. Só nos
    // nós que operam num tempo gráfico (Mercado, Erick); vazio → some (CSS :empty).
    const tfSlot = card.querySelector("[data-tk-tf]");
    if (tfSlot) tfSlot.textContent = stepTfLabel(it);
    // Atribuição por etapa: qual LLM rodou este card (aparece assim que o 1º start
    // reporta o modelo; some se ainda não veio). Atualiza a cada poll.
    const modelSlot = card.querySelector("[data-tk-model]");
    if (modelSlot) modelSlot.textContent = stepModelLabel(it);
    // Parecer que voltou PRONTO do checkpoint numa retomada (task 002): marca de
    // reaproveitado no lugar do selo de modelo — nenhum LLM rodou pra produzi-lo
    // agora, e dizer isso é mais honesto que deixar o card sem explicação.
    const reusedSlot = card.querySelector("[data-tk-reused]");
    if (reusedSlot) reusedSlot.textContent = it.reused ? "reaproveitado" : "";
    const body = card.querySelector(".tk-body");
    // re-renderiza só quando o texto mudou de tamanho (streaming/parcial→final)
    if (body && body.dataset.len !== String(it.len)) {
      body.innerHTML = renderMarkdown(it.text || "");
      body.dataset.len = String(it.len);
    }
  });
}

// Rótulo do LLM que rodou uma etapa: "provider · model" (ex.: "openai · gpt-5.4-mini").
// Vazio quando a atribuição ainda não chegou — nunca inventa (task 024, parte 1).
function stepModelLabel(it) {
  if (!it || !it.model) return "";
  return it.provider ? `${it.provider} · ${it.model}` : it.model;
}

// Selo de TIMEFRAME(s) da etapa (task 009): "semanal · diário" (Mercado) / "4h · 15m"
// (Erick). Vazio nos nós que não operam num tempo gráfico (some via CSS :empty). O TF vem
// do backend (real do motor, não configurado) — nunca inventa aqui.
function stepTfLabel(it) {
  if (!it || !it.timeframe) return "";
  return it.timeframe;
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
// Severidade da inconsistência: PALAVRA com classe de cor (DA-076). A bolinha
// colorida era o único marcador — e bolinha não se lê em leitor de tela nem
// sobrevive a modo de alto contraste.
const _SEV_CLS = { alta: "sev-alta", "média": "sev-media", baixa: "sev-baixa" };
// Carimbo do veredito (task 016): banner âmbar ao lado do veredito quando os insumos
// tinham inconsistência na hora da DECISÃO. O juiz recebeu os DADOS VERIFICADOS e
// decidiu com eles; isto avisa o leitor + lista o que divergiu. Vazio → escondido.
function renderVerdictCaveat(caveat, findings) {
  const el = $("verdictCaveat");
  if (!el) return;
  if (!caveat) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const list = Array.isArray(findings) && findings.length
    ? `<ul class="vc-list">${findings.map((f) =>
        `<li>${escapeHtml((f && f.message) || "")}</li>`).join("")}</ul>`
    : "";
  el.innerHTML = `<span class="vc-head">${escapeHtml(caveat)}</span>${list}`;
  el.classList.remove("hidden");
}

function contradictionsHtml(findings) {
  if (findings === undefined || findings === null) return "";
  if (!Array.isArray(findings) || findings.length === 0) {
    return `<div class="consistency-ok">Checagem de consistência: sem inconsistências ` +
      `(decisão única · gatilho 1-2-3 coerente · preço único · agregados batem).</div>`;
  }
  const items = findings.map((f) => {
    const sev = (f && f.severity) || "";
    const cls = _SEV_CLS[sev] || "sev-baixa";
    return `<li><span class="sev ${cls}">${escapeHtml(sev || "—")}</span> ` +
      `<b>${escapeHtml((f && f.code) || "")}</b>: ` +
      `${escapeHtml((f && f.message) || "")}</li>`;
  }).join("");
  return `<details class="section consistency-warn" open>` +
    `<summary>Checagem de consistência — ${findings.length} inconsistência(s) a revisar</summary>` +
    `<div class="section-body"><ul class="consistency-list">${items}</ul></div></details>`;
}

// Selo de TIMEFRAME(s) no cabeçalho da seção do analista (task 009) — espelha o
// node_timeframe do backend (progress.py) pras seções persistentes do resultado.
// Mercado = semanal · diário (+ frame de referência quando a run é intradiária);
// Erick = 4h · 15m. HONESTO: o que o motor lê, não o configurado. Vazio → não aparece.
function tfTag(node) {
  let tf = "";
  if (node === "market") {
    tf = "semanal · diário";
    if (_verdictTf && _verdictTf !== "1d" && _verdictTf !== "1w") tf += " · " + _verdictTf;
  } else if (node === "erick") {
    tf = "4h · 15m";
  }
  return tf ? ` <span class="sec-tf">${escapeHtml(tf)}</span>` : "";
}

function section(title, mdText, axis, tfNode) {
  if (!mdText || !mdText.trim()) return "";
  return `<details class="section"><summary>${escapeHtml(title)}${tfNode ? tfTag(tfNode) : ""}${axisTag(axis)}</summary>` +
    `<div class="section-body"><div class="md">${renderMarkdown(mdText)}</div></div></details>`;
}

// Fallback transparente (task 027-fallback): selo NA etapa que trocou de provedor
// automaticamente — "⤳ fallback claude-cli → openai (limite/429)". A análise não
// parou; o dono SABE que houve o desvio. Vazio quando a etapa não teve troca.
function stepFallbackBadge(fb) {
  if (!fb || !fb.to_provider) return "";
  const from = fb.from_provider ? escapeHtml(fb.from_provider) : "?";
  const to = escapeHtml(fb.to_provider);
  const why = fb.reason ? ` (${escapeHtml(fb.reason)})` : "";
  const more = fb.hops > 1 ? ` ·${fb.hops}×` : "";
  return ` <span class="as-fallback" title="troca automática de provedor — a análise não parou">` +
    `⤳ fallback ${from} → ${to}${why}${more}</span>`;
}

// Banner de resumo do fallback automático: aparece no topo do resultado quando houve
// pelo menos uma troca de provedor. Diz que a análise NÃO parou e lista os desvios,
// pra a transparência ser VISÍVEL de relance (não escondida no rodapé). Vazio → nada.
function fallbackBannerHtml(fallbacks) {
  if (!Array.isArray(fallbacks) || fallbacks.length === 0) return "";
  const items = fallbacks.map((h) => {
    const from = h.from_provider ? escapeHtml(h.from_provider) : "?";
    const to = h.to_provider ? escapeHtml(h.to_provider) : "?";
    const why = h.reason ? ` — motivo ${escapeHtml(h.reason)}` : "";
    const step = h.node ? `<span class="fb-step">${escapeHtml(h.node)}</span> ` : "";
    return `<li>${step}${from} → <b>${to}</b>${why}</li>`;
  }).join("");
  const n = fallbacks.length;
  return `<div class="section fallback-banner">` +
    `<div class="fb-head">⤳ Fallback automático — ${n} troca(s) de provedor. ` +
    `A análise <b>não parou</b>: caiu pro próximo provedor saudável e concluiu.</div>` +
    `<ul class="fb-list">${items}</ul></div>`;
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
  // Atribuição POR ETAPA (task 024, parte 1): qual LLM rodou cada etapa (real, do
  // callback). Detalhe colapsável — o resumo por camada fica na linha; o por-etapa
  // abre embaixo. Só aparece quando o run trouxe a lista (vazia em reaproveitado).
  const byStep = Array.isArray(audit.models_by_step) ? audit.models_by_step : [];
  const byStepHtml = byStep.length
    ? `<details class="audit-steps"><summary>qual LLM fez cada etapa (${byStep.length})</summary>` +
      `<ul class="audit-steps-list">` +
      byStep.map((s) => {
        const lbl = stepModelLabel(s);
        // TF real da etapa (task 009) ao lado do modelo — só onde se aplica (Mercado/Erick).
        const tf = s.timeframe ? `<span class="as-tf">${escapeHtml(s.timeframe)}</span>` : "";
        return `<li><span class="as-step">${escapeHtml(s.label || s.node || "—")}</span>` +
          `<span class="as-meta">${tf}<span class="as-model">${lbl ? escapeHtml(lbl) : "—"}</span></span>` +
          stepFallbackBadge(s.fallback) + `</li>`;
      }).join("") +
      `</ul></details>`
    : "";
  return `<div class="audit-footer">` +
    `run ${escapeHtml(audit.run_id)} · coleta ${escapeHtml(audit.collected_at || "—")} · ` +
    `pipeline v${escapeHtml(audit.pipeline_version || "—")}${price}` +
    (models ? ` · modelos: ${models}` : "") +
    byStepHtml +
    `</div>`;
}

// Banner de erro HUMANO: mensagem acionável em pt-BR (nunca stack, nunca chave).
// Erros de chave/crédito (no_credit/invalid_key) ganham um botão que abre o painel
// de Configurações; os demais só a mensagem + dica de tentar de novo.
const _CFG_ERROR_CODES = new Set(["no_credit", "invalid_key"]);
function errorCardHtml(message, code, runId) {
  const msg = message || "Falha ao rodar a análise.";
  const wantsConfig = _CFG_ERROR_CODES.has(code);
  const action = wantsConfig
    ? `<button type="button" class="err-action" data-act="open-config">Abrir Configurações</button>`
    : `<span class="err-hint">Você pode tentar de novo pelos botões de método/timeframe acima.</span>`;
  return `<div class="error-card ${escapeHtml(code || "error")}">` +
    `<div class="err-title">Não deu pra concluir</div>` +
    `<div class="err-msg">${escapeHtml(msg)}</div>` +
    `<div class="err-foot">${action}</div>` +
    escalateBoxHtml(runId) +
    `</div>`;
}

// Escalonamento de etapa (task 027 parte B): SÓ o dono vê. Re-roda a etapa que
// falhou com outro provedor+modelo, reaproveitando o checkpoint (022). O servidor
// barra se a run não é retomável (BYOK) — a mensagem honesta aparece aqui.
function escalateBoxHtml(runId) {
  if (!_isOwner || !runId) return "";
  const provs = ((_llmMeta && _llmMeta.providers) || [])
    .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label)}</option>`).join("");
  return `<div class="err-escalate" data-run-id="${escapeHtml(runId)}">
    <div class="err-escalate-title">Escalar uma etapa com outro LLM</div>
    <div class="err-escalate-row">
      <select data-esc="level" aria-label="Nível a escalar">
        <option value="quick">Rápido (analistas · debate)</option>
        <option value="deep">Pesado (pesquisa · juiz)</option>
      </select>
      <select data-esc="provider" aria-label="Provedor">${provs}</select>
      <input data-esc="model" type="text" autocomplete="off" placeholder="modelo (opcional)" />
      <button type="button" class="err-action" data-act="escalate">Escalar etapa</button>
    </div>
    <span class="cfg-status err-escalate-status" data-esc="status"></span>
  </div>`;
}

function bindErrorCard(container) {
  const btn = container && container.querySelector('[data-act="open-config"]');
  if (btn) btn.addEventListener("click", () => {
    $("configPanel").classList.remove("hidden");
    scrollToOpen($("configPanel"));
  });
  const esc = container && container.querySelector('[data-act="escalate"]');
  if (esc) esc.addEventListener("click", () => escalateStep(container));
}

// ---- Erro PARCIAL: preserva as etapas concluídas (task 015) -----------------
// Um erro no meio NÃO zera a análise: o backend monta um result parcial com o que já
// rodou (analistas + debate). Aqui a UI mostra essas etapas + um banner "parou nesta
// etapa" com o caminho pra CONTINUAR do ponto (escalar 027 / retomar 022), em vez da
// tela vazia de "ERRO" que descartava tudo.
function _hasAnyReport(r) {
  if (!r) return false;
  return ["market_report", "sentiment_report", "news_report", "fundamentals_report",
    "erick_report", "bull", "bear", "research_manager", "investment_plan",
    "trader_plan", "risk_decision"].some((k) => (r[k] || "").toString().trim());
}

// Linha de metadados (data+conclusão · tipo · custo · tempo) — mesma do sucesso.
// Data da análise e o carimbo de quando parou são quase o mesmo dado (task 037):
// viram UMA unidade em vez de duas quase-iguais competindo por atenção.
function resultMetaHtml(snap) {
  const finished = snap.finished_at || (snap.result && snap.result.finished_at);
  return `<span>Análise <b>${escapeHtml(snap.date || "")}</b>` +
      (finished ? ` · interrompida <b>${fmtStamp(finished, true)}</b>` : "") +
    `</span>` +
    `<span>Tipo <b>${escapeHtml(assetPt(snap.asset_type))}</b></span>` +
    `<span>Custo <b>${fmtCost(snap.cost)}</b></span>` +
    `<span>Tempo <b>${snap.elapsed || 0}s</b></span>`;
}

// Banner do erro PARCIAL: nomeia a etapa que falhou, diz que o resto está preservado
// abaixo, e traz a ação de continuar (abrir config quando é chave/crédito + escalar).
function partialBannerHtml(snap, r) {
  const step = (r.failed_step && r.failed_step.label) ? escapeHtml(r.failed_step.label) : "";
  const msg = snap.error || "Uma etapa falhou.";
  const wantsConfig = _CFG_ERROR_CODES.has(snap.error_code);
  const action = wantsConfig
    ? `<div class="err-foot"><button type="button" class="err-action" data-act="open-config">Abrir Configurações</button></div>`
    : "";
  const stepLine = step
    ? `<div class="err-msg">Parou em: <b>${step}</b>. As etapas concluídas abaixo estão <b>preservadas</b> — continue do ponto (escale a etapa ou retome), sem refazer tudo.</div>`
    : `<div class="err-msg">As etapas concluídas abaixo estão <b>preservadas</b> — continue do ponto, sem refazer tudo.</div>`;
  return `<div class="error-card partial ${escapeHtml(snap.error_code || "error")}">` +
    `<div class="err-title">Parou nesta etapa — o já feito foi preservado</div>` +
    `<div class="err-msg">${escapeHtml(msg)}</div>` +
    stepLine + action +
    escalateBoxHtml(snap.run_id) +
    `</div>`;
}

// Monta o corpo do erro parcial: banner no topo + as etapas concluídas (as mesmas
// seções do sucesso, só as que têm texto) + rodapé de auditoria parcial.
function partialReportsHtml(snap, r) {
  const isCrypto = snap.asset_type === "crypto";
  const axes = r.axes || {};
  let html = partialBannerHtml(snap, r);
  html += fallbackBannerHtml(r.fallbacks);
  if (r.erick_report && r.erick_report.trim()) {
    html += `<details class="section erick" open><summary>Método Erick — recuo à média · saída · peso do trade${tfTag("erick")}${axisTag(axes.erick)}</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.erick_report)}</div></div></details>`;
  }
  html += section("Juiz do Debate (Gestor de Pesquisa) — leitura", r.research_manager || r.investment_plan, axes.juiz);
  html += section("Mercado — preço e múltiplos tempos gráficos", r.market_report, axes.tecnico, "market");
  html += section("Notícias — macro e mercados de previsão", r.news_report);
  html += section("Sentimento", r.sentiment_report);
  if (!isCrypto) html += section("Fundamentos", r.fundamentals_report);
  html += section("Plano do Trader (leitura — insumo, não é o veredito)", r.trader_plan, axes.trader);
  html += section("Decisão de Risco (parcial)", r.risk_decision);
  html += auditFooterHtml(r.audit, null);
  return html;
}

// Retomar do ponto (022) numa run que ERROU e é resumível (dono/servidor + checkpoint):
// o resume continua do último nó concluído, reaproveitando o que já rodou. BYOK não é
// resumível — nesses casos a barra some (o parcial fica; o full re-run é só por ação).
function maybeShowErrorResume(snap) {
  const bar = $("resumeBar");
  if (!bar) return;
  if (snap.resumable && snap.run_id) {
    $("resumeMsg").textContent = "Retome do ponto que falhou — reaproveita as etapas já concluídas.";
    bar.dataset.runId = snap.run_id;
    bar.classList.remove("hidden");
  } else {
    bar.classList.add("hidden");
  }
}

// POST /api/run/<id>/escalate: re-roda SÓ a etapa escolhida com o outro LLM. O
// servidor é owner-gated e recusa run não-resumível (BYOK) com mensagem honesta.
async function escalateStep(container) {
  const box = container.querySelector(".err-escalate");
  if (!box) return;
  const runId = box.dataset.runId;
  const level = box.querySelector('[data-esc="level"]').value;
  const provider = box.querySelector('[data-esc="provider"]').value;
  const model = box.querySelector('[data-esc="model"]').value.trim();
  const status = box.querySelector('[data-esc="status"]');
  status.textContent = "escalando…"; status.className = "cfg-status err-escalate-status";
  try {
    const res = await fetch("/api/run/" + encodeURIComponent(runId) + "/escalate", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, provider, model }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      status.textContent = "re-rodando a etapa (reaproveita o que já rodou)…";
      status.classList.add("ok");
      watchRun(runId);
    } else {
      status.textContent = (data && data.error) || "não deu pra escalar";
      status.classList.add("err");
    }
  } catch (e) {
    status.textContent = "erro de rede ao escalar";
    status.classList.add("err");
  }
}

function renderResult(snap) {
  // este run passa a ser o "aberto" na tela: enquanto for ele, um término dele
  // NÃO vira aviso "pronto" (o usuário já está vendo o resultado).
  clearActiveRun();   // resultado na tela = nada de run vivo a reengatar
  _watchedRunId = snap.run_id || _watchedRunId;
  // PARAR/PAUSAR (task 026): run cancelada não tem resultado — libera a UI com um aviso
  // honesto (não é erro, não abre painel de resultado vazio). Vale pra todo caller.
  if (snap.status === "cancelled") { renderCancelled(snap); return; }
  // Terminou (done/error) antes do cancel pegar: solta a trava do 'parando…' (013).
  if (_cancelPending && _cancelPending === (snap.run_id || _watchedRunId)) clearCancelPending();
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
  // Esconde uma barra de Retomar remanescente; o erro PARCIAL resumível a re-mostra.
  if ($("resumeBar")) $("resumeBar").classList.add("hidden");

  if (snap.status === "error") {
    const er = snap.result || {};
    const hasPartial = er.partial === true || _hasAnyReport(er);
    $("chartCard").classList.add("hidden");
    $("setupCards").classList.add("hidden");
    // A ESCADA some junto: ela pertence ao resultado, e o resultado anterior ainda
    // estar na tela sob um erro faria a análise que FALHOU parecer ter cinco leituras.
    renderEscada(null);
    // …e a nota de revalidação também: ela descreve uma leitura que existia.
    _revalNota = null;
    _openDegraded = null;       // …e a lista degradada, que é da run que não existe mais
    $("revalLinha").classList.add("hidden");
    $("headPrice").classList.add("hidden");
    $("headLevels").classList.add("hidden");   // run com erro não tem gatilho nem cotação a mostrar
    $("verdictTf").classList.add("hidden");
    $("degradedBanner").classList.add("hidden");
    $("exportPdfBtn").classList.add("hidden");  // nada de análise pra exportar num run com erro
    $("confrontCtl").classList.add("hidden");   // não confrontar a partir de um run com erro
    // Reanálise segue disponível pela barra ÚNICA: uma falha (fonte fora do ar,
    // transitório) é justamente quando o usuário quer rerodar escolhendo método/TF.
    // Método aberto: preserva o que o run errado carregava (history traz r.method);
    // sem isso, cai em padrão (não inventa método num run que falhou). setup123
    // ENTRA na lista: um atalho $0 que falhou tem que voltar como atalho — sem isto
    // o ↻ do erro caía em "padrao" e cobrava uma análise completa de LLM.
    const snapM = _METODOS_CONHECIDOS.has(snap.method) ? snap.method : null;
    _openMethod = (snapM && snapM !== "compare") ? snapM : _openMethod;
    _openView = snapM || "";
    _openDate = snap.date || "";
    _assetType = snap.asset_type || "";
    // Escada completa: intradiário vale pra ação e cripto (fonte real keyless dos
  // dois; frame sem candle degrada honesto sob demanda). Só o fallback — a fonte
  // da verdade é result.timeframes do backend.
  _timeframes = ALL_TFS.map(([t]) => t);
    _verdictTf = snap.verdict_timeframe || "1d";
    syncLaunchBarToOpen();
    $("bull").innerHTML = ""; $("bear").innerHTML = "";
    $("bullLead").textContent = ""; $("bearLead").textContent = "";
    if (hasPartial) {
      // PRESERVA o trabalho (task 015): mostra as etapas concluídas + banner "parou
      // nesta etapa" + escalar/retomar do ponto — NUNCA tela vazia que zera tudo.
      $("verdictBadge").className = "verdict hold";
      $("verdictBadge").textContent = "PARCIAL";
      $("resultMeta").innerHTML = resultMetaHtml(snap);
      const railP = document.querySelector(".rail-theses");
      if (railP) railP.classList.remove("hidden");
      renderThesis("bull", er.bull);
      renderThesis("bear", er.bear);
      $("sections").innerHTML = partialReportsHtml(snap, er);
      bindErrorCard($("sections"));
      maybeShowErrorResume(snap);   // Retomar do ponto (022) numa run resumível (dono)
      mountAskBox($("askSingle"), snap.run_id);  // dá pra perguntar sobre o que já rodou
      scrollToOpen(panel);
      loadHistory();
      return;
    }
    // Nada concluído: erro honesto (sem parcial). Banner + escalar, sem teses vazias.
    $("verdictBadge").className = "verdict sell";
    $("verdictBadge").textContent = "ERRO";
    $("resultMeta").innerHTML = "";
    const railTheses = document.querySelector(".rail-theses");
    if (railTheses) railTheses.classList.add("hidden");
    if ($("resumeBar")) $("resumeBar").classList.add("hidden");
    // Banner de erro HUMANO (sem stack, sem chave): a mensagem acionável do backend
    // + botão pra abrir as Configurações quando é problema de chave/crédito.
    $("sections").innerHTML = errorCardHtml(snap.error, snap.error_code, snap.run_id);
    bindErrorCard($("sections"));
    mountAskBox($("askSingle"), "");  // run com erro sem parcial não tem o que ancorar
    return;
  }

  const r = snap.result || {};
  // reexibe as teses (podem ter sido escondidas por um render de erro anterior).
  const railThesesOk = document.querySelector(".rail-theses");
  if (railThesesOk) railThesesOk.classList.remove("hidden");
  // método da análise aberta: a estrutura (recuo/1-2-3) é EMA 8/21 no Erick, MMS no
  // Padrão. Trocar de TF precisa recalcular na mesma família — daí guardar o método.
  // setup123 (atalho estrutural $0) é método próprio — o ↻ re-roda o atalho, não a
  // análise completa.
  _openMethod = r.storm123 ? "storm123"
    : r.setup123 ? "setup123"
    : ((r.erick_report && r.erick_report.trim()) ? "erick" : "padrao");
  _openView = _openMethod;   // a barra destaca o método aberto (Padrão/Erick/1-2-3/Storm)
  const estrutural = _METODOS_ESTRUTURAIS.has(_openMethod);
  $("verdictBadge").className = estrutural ? `verdict ${_openMethod}` : verdictClass(r.verdict);
  $("verdictBadge").innerHTML = estrutural
    ? escapeHtml(methodLabel(_openMethod)) : verdictHtml(r.verdict);
  renderVerdictCaveat(r.verdict_caveat, r.pre_judge_findings);
  // Data da análise + carimbo de conclusão viram UMA unidade (task 037: eram duas
  // quase-iguais). O nome do método some daqui pros estruturais — já está grande
  // no badge do veredito acima; a explicação longa fica só pra quem abre o detalhe.
  const finished = snap.finished_at || (snap.result && snap.result.finished_at);
  $("resultMeta").innerHTML =
    `<span>Análise <b>${escapeHtml(snap.date || "")}</b>` +
      (finished ? ` · concluída <b>${fmtStamp(finished, true)}</b>` : "") +
    `</span>` +
    `<span>Tipo <b>${escapeHtml(assetPt(snap.asset_type))}</b></span>` +
    (r.storm123 ? `<span>Método <b>Storm123 + Éden — leitura estrutural, sem LLM</b></span>` : "") +
    (r.setup123 ? `<span>Método <b>Setup123 — leitura estrutural, sem LLM</b></span>` : "") +
    `<span>Custo <b>${fmtCost(snap.cost)}</b></span>` +
    `<span>Tempo <b>${snap.elapsed || 0}s</b></span>`;

  // Estado do seletor de timeframe do ativo aberto. Operabilidade é propriedade do
  // ATIVO HOJE, não um congelado da run: ação e cripto têm a escada intradiária
  // inteira agora, então uma run ANTIGA (salva quando ação só tinha 1w/1d) também
  // ganha os botões intradiários ao reabrir. O backend (/api/chart) é o árbitro
  // real e degrada honesto por símbolo/data. Toda análise começa exibindo o diário.
  _openDate = snap.date || "";
  _assetType = snap.asset_type || "";
  _tf = r.timeframe || "1d";
  _timeframes = ALL_TFS.map(([t]) => t);
  // TF em que o VEREDITO foi computado (carimbo do cabeçalho). Runs antigas não
  // têm o campo → cai no frame do gráfico. É diferente de _tf: _tf pode ser
  // trocado só pra olhar o gráfico, o carimbo fixa o frame do veredito real.
  _verdictTf = snap.verdict_timeframe || r.verdict_timeframe || r.timeframe || "1d";
  renderVerdictTf();
  syncLaunchBarToOpen();              // a barra passa a apontar pro aberto (método + frame do veredito)
  renderDegraded(r.degraded);
  hideDegrade();

  // As camadas: padrão do método enquanto ele não tocou no seletor; a escolha DELE
  // daí em diante (a 009 zerava a cada análise — o Samyr pediu o contrário, e ele
  // está certo: reconfigurar a cada tela é transformar preferência em tarefa).
  iniciaCamadas(r.actionable);
  // Análise NOVA na tela: a nota de revalidação e o memo da cotação são da anterior.
  // Manter qualquer um deles faria a tela carimbar uma hora que não é desta leitura.
  _revalNota = null;
  _revalCota = null;
  _revalVoo = null;
  _openLive = r.live_price || null;
  renderHeadPrice(r.actionable, _openLive);
  renderSetupCards(r.actionable);
  // A ESCADA vem do RESULTADO da run (foi computada junto da análise, $0 de LLM):
  // uma análise antiga, sem o campo, simplesmente não a mostra — nada de escada
  // vazia nem de recomputar por trás pra fingir que sempre existiu.
  renderEscada(r.multiframe);
  const desenhouRun = renderChartCard(r.price_chart, snap.ticker, r.actionable,
                                     r.timeframe || snap.timeframe);
  if (desenhouRun) _tfDesenhado = r.timeframe || snap.timeframe || _tf;
  declaraFrameDoGrafico(_tf, desenhouRun);
  renderTfSelector();
  carregaExecCard();
  // Análise aberta: a revalidação automática passa a acompanhar o candle DESTE
  // frame (DA-118). Sem isto, ela só existiria depois de uma troca manual.
  agendaProximaRevalidacao();

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
  // Fallback transparente (task 027-fallback): se o motor trocou de provedor sozinho
  // em alguma etapa, o banner de resumo abre logo abaixo do QA — visível de relance.
  html += fallbackBannerHtml(r.fallbacks);
  if (r.erick_report && r.erick_report.trim()) {
    html += `<details class="section erick" open><summary>Método Erick — recuo à média · saída · peso do trade${tfTag("erick")}${axisTag(axes.erick)}</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.erick_report)}</div></div></details>`;
  }
  // For crypto, the deterministic derivatives feed goes first and open — it is
  // the data yfinance can't see and the source is always named here.
  if (isCrypto && r.derivatives_report && r.derivatives_report.trim()) {
    html += `<details class="section" open><summary>Derivativos — taxa de financiamento <span class="orig">(funding)</span> · contratos em aberto <span class="orig">(open interest)</span> · liquidações <span class="orig">(liquidations)</span> (fonte nomeada)</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.derivatives_report)}</div></div></details>`;
  }
  html += section("Juiz do Debate (Gestor de Pesquisa) — leitura", r.research_manager || r.investment_plan, axes.juiz);
  html += section("Mercado — preço e múltiplos tempos gráficos", r.market_report, axes.tecnico, "market");
  html += section("Notícias — macro e mercados de previsão", r.news_report);
  html += section("Sentimento", r.sentiment_report);
  if (!isCrypto) html += section("Fundamentos", r.fundamentals_report);
  html += section("Plano do Trader (leitura — insumo, não é o veredito)", r.trader_plan, axes.trader);
  html += section("Decisão de Risco (veredito final na íntegra — a única decisão)", r.risk_decision || r.final_trade_decision, axes.veredito);
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
  const title = "" + (c.label || (isErick ? "Método Erick" : "Padrão"));
  const v = c.verdict || (c.status === "error" ? "error" : "");
  const plan = isErick
    ? (c.erick_report || c.trader_plan || c.final_decision || "")
    : (c.trader_plan || c.final_decision || "");
  const reused = c.reused
    ? `<span class="cmp-reused" title="reaproveitado do cache — não re-rodou">cache</span>`
    : "";
  const dateStr = c.date ? `<span class="cmp-col-date">${escapeHtml(fmtDate(c.date))}</span>` : "";
  const openBtn = c.run_id
    ? `<button type="button" class="cmp-open" data-id="${escapeHtml(c.run_id)}">abrir análise completa →</button>`
    : "";
  // Mesma separação do banner: "feito sem" só vale pra fonte AUSENTE; turno com
  // texto sinalizado entrou na leitura e é anunciado como tal.
  const degItems = (Array.isArray(c.degraded) ? c.degraded : []).filter(Boolean);
  const degMissing = degItems.filter((d) => d.kind !== "suspect");
  const degSuspect = degItems.filter((d) => d.kind === "suspect");
  const deg =
    (degMissing.length ? `<div class="cmp-degraded">Feito sem: ${degMissing.map(degradedName).join(" · ")}</div>` : "") +
    (degSuspect.length ? `<div class="cmp-degraded">Texto sinalizado: ${degSuspect.map(degradedName).join(" · ")}</div>` : "");
  const err = c.status === "error"
    ? `<div class="cmp-err">Leitura indisponível: ${escapeHtml(c.error || "falha")}</div>`
    : "";
  const ch = c.price_chart;
  const hasChart = ch && Array.isArray(ch.candles) && ch.candles.length > 2;
  const chartCard = hasChart
    ? `<div class="cmp-chart-card">` +
        `<div class="chart-legend cmp-chart-legend">${chartLegendHtml(ch, c.actionable)}</div>` +
        `<div class="chart-wrap">` +
          // A dica descreve o gesto REAL (DA-122). Ela prometia "arrasta = move 2
          // eixos" quando arrastar não fazia nada sem zoom prévio, e chamava o
          // duplo-clique de "reseta" — que não diz o que se ganha com ele. Agora
          // arrastar vale desde o começo, e o duplo-clique é o "ajustar à tela"
          // que traz velas E níveis do plano de volta ao enquadramento.
          `<span class="chart-zoom-hint">roda=zoom · régua direita=zoom vertical · régua de baixo=zoom horizontal · arrasta=move o gráfico · 2 cliques=ajusta à tela (velas + níveis do plano)</span>` +
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
  const agrLabel = agr === "concordam" ? "Concordam"
    : (agr === "divergem" ? "Divergem"
      : (agr === "invalido" ? "Inválido" : "Parcial"));
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

  // Barra ÚNICA também na comparação: o ativo está aberto, então a barra aponta pra
  // ele (método = Comparar destacado, TF de referência = lado A, senão B, senão diário)
  // e dá pra rerodar sem redigitar. Estado do ativo aberto vem do snapshot do compare.
  _openTicker = snap.ticker || "";
  _openDate = snap.date || "";
  _assetType = snap.asset_type || "";
  _openView = "compare";   // a barra destaca "Comparar" enquanto a comparação está aberta
  // Escada completa: intradiário vale pra ação e cripto (fonte real keyless dos
  // dois; frame sem candle degrada honesto sob demanda). Só o fallback — a fonte
  // da verdade é result.timeframes do backend.
  _timeframes = ALL_TFS.map(([t]) => t);
  const cmpTf = (a && (a.verdict_timeframe || a.timeframe)) ||
    (b && (b.verdict_timeframe || b.timeframe)) || "1d";
  _verdictTf = cmpTf;
  syncLaunchBarToOpen();

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
    `<div class="ask-head">Pergunte sobre esta análise</div>` +
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
  const tf = tfNome(r.verdict_timeframe || "1d");
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

// ---- barra ÚNICA: timeframe + método + chip de data + ↻ (task 029) ----------
// O launcher absorveu a barra de reanálise (023): um só lugar pro método e o TF.
// Analisar (startAnalysis) roda o ticker do INPUT com o método + TF escolhidos aqui;
// ↻ (runReanalyze via _openView) reanalisa o ativo ABERTO hoje preservando o método
// aberto. Os frames operáveis vêm do ativo aberto (o backend é a fonte da verdade);
// sem ativo aberto, a escada inteira fica ativa (o backend clampa/degrada honesto).
let _barTf = "1d";        // timeframe escolhido na barra (default diário; reflete o veredito do aberto)
let _barMethod = "padrao"; // método escolhido na barra: "padrao" | "erick" | "compare"

// Normaliza um "view" (padrao|erick|compare|"") pro método a rodar. "" (run com erro
// sem método) e qualquer desconhecido caem em padrão — nunca inventa método.
// Métodos que o backend conhece — usado pra preservar o método de um run que
// falhou sem inventar nada (desconhecido/ausente = "", cai no padrão).
const _METODOS_CONHECIDOS = new Set(["padrao", "erick", "setup123", "storm123", "compare"]);

// Métodos ESTRUTURAIS ($0 de LLM). São SEPARADOS, não flags um do outro: o 1-2-3
// deste projeto e o 1-2-3 Storm usam a mesma numeração pra pontos DIFERENTES
// (ver DA-081) — o que eles compartilham é só não custar nada.
const _METODOS_ESTRUTURAIS = new Set(["setup123", "storm123"]);

function normMethod(v) {
  if (v === "compare") return "compare";
  if (v === "erick") return "erick";
  if (_METODOS_ESTRUTURAIS.has(v)) return v;
  return "padrao";
}
// RÓTULO de tela, não identificador. Os dois métodos SÃO um 1-2-3 — a diferença
// está em QUAL —, e "1-2-3" × "Storm" obrigava a lembrar de qual era qual. "Setup123"
// e "Storm123" nomeiam sozinhos. O valor interno (`setup123`/`storm123`) fica intacto:
// ele é o que já está gravado no histórico, no reúso e no ledger do track record.
function methodLabel(v) {
  if (v === "compare") return "Comparar";
  if (v === "erick") return "Erick";
  if (v === "setup123") return "Setup123";
  if (v === "storm123") return "Storm123";
  return "Padrão";
}

// Chip de data: "Hoje" quando a data é a de hoje (ou vazia), senão dd/mm. O input
// #date (sobreposto transparente) é a fonte da verdade; o chip só espelha o rótulo.
function updateDateChip() {
  const el = $("dateChipLabel");
  if (!el) return;
  const v = ($("date") && $("date").value) || "";
  const today = _todayManaus || new Date().toLocaleDateString("en-CA");
  el.textContent = (!v || v === today) ? "Hoje" : fmtDate(v);
}

function renderLaunchBar() {
  const tfsEl = $("launchTfs");
  const mEl = $("launchMethods");
  if (!tfsEl || !mEl) return;
  // Com um ativo aberto, os frames operáveis são os dele; sem ativo aberto (launch
  // de um ticker novo), a escada inteira fica selecionável — o backend não inventa
  // candle e degrada honesto por símbolo/data.
  const enabled = _openTicker ? new Set(_timeframes || ["1d"]) : new Set(ALL_TFS.map(([t]) => t));
  if (!enabled.has(_barTf)) _barTf = enabled.has(_verdictTf) ? _verdictTf : "1d";
  const pill = ([tf, curto, completo]) => {
    const on = enabled.has(tf);
    const active = tf === _barTf;
    const cls = ["lb-tf", active ? "is-active" : "", on ? "" : "is-off"].filter(Boolean).join(" ");
    const title = on ? `Analisar no ${completo}` : "Frame indisponível para este ativo (o backend não inventa candle)";
    // O botão MOSTRA o curto e SE CHAMA pelo completo: quem lê de relance ganha a
    // largura, quem usa leitor de tela (ou passa o mouse) continua ouvindo "Semanal".
    return `<button type="button" class="${cls}" data-tf="${tf}" ${on ? "" : "disabled"} ` +
      `title="${escapeHtml(title)}" aria-label="${escapeHtml(completo)}">${escapeHtml(curto)}</button>`;
  };
  // TEMPO em DUAS linhas contando como UM elemento da barra — a mesma gramática do
  // bloco MODELOS (coluna de duas fileiras, alinhada embaixo com o resto). Em cima
  // o macro (S · D), embaixo o intradiário (4h · 1h · 15m).
  tfsEl.innerHTML = tfFaixas().map(({ faixa, itens }) =>
    `<div class="lb-tf-row is-${faixa}">${itens.map(pill).join("")}</div>`).join("");
  // MÉTODO em DUAS fileiras contando como UM elemento da barra — a mesma gramática
  // do TEMPO e do bloco MODELOS. Com o Storm são CINCO métodos, e cinco numa fila só
  // empurravam a barra além dos 1440 (o ATIVO encolhia pra pagar a conta).
  //
  // A divisão não é só de espaço: em cima os que rodam MODELO (custam), embaixo os
  // ESTRUTURAIS (leem a série, $0). A largura do grupo passa a ser a da fileira mais
  // larga em vez da soma das cinco.
  const methodRows = [
    ["llm", [
      ["padrao", "Padrão", "Leitura Padrão (MMS · 1-2-3) no timeframe escolhido"],
      ["erick", "Erick", "Método Erick — recuo à média, saída antes da reversão, peso do trade"],
      ["compare", "Comparar", "Roda as DUAS (Padrão e Erick) e confronta com o meta-juiz — a divergência é o sinal"],
    ]],
    ["estrutural", [
      ["setup123", "Setup123", "Só o setup estrutural: gatilho, invalidação, SL, TP e R:R — sem LLM, instantâneo ($0)"],
      ["storm123", "Storm123", "O 1-2-3 do Stormer com filtro Éden (MME 8 × MME 80): ponto 2 é o FUNDO, stop no ponto 2, alvo por projeção da amplitude — sem LLM ($0)"],
    ]],
  ];
  mEl.innerHTML = methodRows.map(([faixa, itens]) =>
    `<div class="lb-method-row is-${faixa}">` + itens.map(([m, label, title]) => {
      const active = m === _barMethod;
      const cls = ["lb-method", m, active ? "is-active" : ""].filter(Boolean).join(" ");
      return `<button type="button" class="${cls}" data-method="${m}" aria-pressed="${active ? "true" : "false"}" title="${escapeHtml(title)}">${escapeHtml(label)}</button>`;
    }).join("") + "</div>").join("");
  updateDateChip();
  renderLaunchModels();
  const rerun = $("rerunBtn");
  if (rerun) {
    rerun.disabled = !_openTicker;
    rerun.title = _openTicker
      ? `Reanalisar ${_openTicker} hoje (método ${methodLabel(_openView)})`
      : "Abra um ativo pra reanalisar";
  }
}

// ---- Seletor compacto de modelos no launcher (task 012) ---------------------
// O modelo rápido/pesado vivia SÓ dentro do #configPanel, atrás do botão "Chaves" —
// quem procurava "escolher modelo" nunca achava (mesmo padrão do botão Parar que
// estava escondido). Aqui os dois chips ficam na PRÓPRIA barra de análise, mostram o
// modelo EM USO e abrem um popover pesquisável pra ESCOLHER da lista real do provedor
// (não digitar). A escolha aplica na hora, persiste em _llmCfg e espelha nos campos
// do config (#cfgQuick/#cfgDeep). O cross-provider por nível (task 027) segue no
// config, alcançável pelo link "avançado" do popover — mantido "junto".

// Provedor efetivo de um nível: cada nível tem o seu (task 017). Config antiga
// (provedor único, pré-017) cai no `provider` salvo — migra sozinha ao abrir o painel.
function _effLevelProvider(level) {
  const c = _llmCfg || {};
  return (level === "deep" ? c.deepProvider : c.quickProvider) || c.provider ||
    (_llmMeta && _llmMeta.default_provider) || "";
}

// Modelo padrão do provedor pra um nível (mostrado quando o campo está vazio).
function _providerDefaultModel(level) {
  const p = _providerMeta(_effLevelProvider(level));
  if (p) return (level === "deep" ? p.default_deep : p.default_quick) || "";
  return _llmMeta ? ((level === "deep" ? _llmMeta.default_deep : _llmMeta.default_quick) || "") : "";
}

// Nome curto pra caber no chip (cauda após a última "/"); o título mantém o id inteiro.
function _shortModel(id) {
  const s = String(id || "");
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(i + 1) : s;
}

function renderLaunchModels() {
  const host = $("launchModels");
  if (!host) return;
  const chip = (level) => {
    const icon = level === "deep" ? "pesado" : "rápido";
    const lead = level === "deep" ? "pesado" : "rápido";
    const set = (level === "deep" ? _llmCfg.deepModel : _llmCfg.quickModel) || "";
    const eff = set || _providerDefaultModel(level);
    const shown = eff ? _shortModel(eff) : "padrão";
    const prov = _effLevelProvider(level);
    const title = `Modelo ${lead}${prov ? " · " + prov : ""}: ${eff || "padrão do provedor"} — clique pra escolher`;
    const cls = ["lb-model-pick", set ? "" : "is-default"].filter(Boolean).join(" ");
    return `<button type="button" class="${cls}" data-level="${level}" title="${escapeHtml(title)}" aria-haspopup="listbox">`
      + `<span class="lbm-icon" aria-hidden="true">${icon}</span>`
      + `<span class="lbm-lead">${lead}</span>`
      + `<span class="lbm-model">${escapeHtml(shown)}</span></button>`;
  };
  host.innerHTML = chip("quick") + chip("deep");
}

// Popover pesquisável (singleton) ancorado no chip. Reusa filterModels/_priceLabel/
// _modelItems — a MESMA lista do config; se ainda não veio, dispara refreshModels().
let _lbPop = null;   // { el, level, input, list, view, active }

function _closeLaunchModelPop() {
  if (_lbPop && _lbPop.el && _lbPop.el.parentNode) _lbPop.el.parentNode.removeChild(_lbPop.el);
  document.removeEventListener("mousedown", _lbPopOutside, true);
  document.removeEventListener("keydown", _lbPopEsc, true);
  _lbPop = null;
}
function _lbPopOutside(e) {
  if (!_lbPop) return;
  if (!_lbPop.el.contains(e.target) && !e.target.closest(".lb-model-pick")) _closeLaunchModelPop();
}
function _lbPopEsc(e) {
  if (e.key === "Escape" && _lbPop) { e.preventDefault(); e.stopPropagation(); _closeLaunchModelPop(); }
}

// Aplica (ou limpa, val="") o modelo do nível: na hora, persiste e espelha no config.
function _lbChooseModel(level, val) {
  const key = level === "deep" ? "deepModel" : "quickModel";
  _llmCfg = _llmCfg || {};
  _llmCfg[key] = val || "";
  saveLlmCfg(_llmCfg);
  const cfgId = level === "deep" ? "cfgDeep" : "cfgQuick";
  if ($(cfgId)) $(cfgId).value = val || "";       // espelha no campo do config
  updateConfigBadge();
  renderLaunchModels();
  _closeLaunchModelPop();
}

function _lbRenderPopList() {
  const p = _lbPop; if (!p) return;
  // Modelos do provedor DESTE nível (task 014): ao vivo se buscado, senão o catálogo.
  const items = _itemsForProvider(p.prov);
  p.view = filterModels(items, p.input.value).slice(0, MODEL_LIST_MAX);
  p.active = -1;
  if (!items.length) {
    p.list.innerHTML = `<li class="combo-empty" aria-disabled="true">carregando modelos… (Enter usa o texto digitado)</li>`;
    return;
  }
  if (!p.view.length) {
    p.list.innerHTML = `<li class="combo-empty" aria-disabled="true">nenhum modelo casa — Enter usa o texto digitado</li>`;
    return;
  }
  p.list.innerHTML = p.view.map((it, i) => {
    const price = _priceLabel(it);
    const name = (it.name && it.name !== it.id) ? `<span class="combo-name">${escapeHtml(it.name)}</span>` : "";
    const priceEl = price ? `<span class="combo-price">${escapeHtml(price)}</span>` : "";
    return `<li class="combo-opt${i === p.active ? " is-active" : ""}" role="option" data-val="${escapeHtml(it.id)}">`
      + `<span class="combo-id">${escapeHtml(it.id)}</span>${name}${priceEl}</li>`;
  }).join("");
}

function _lbPopMove(delta) {
  const p = _lbPop; if (!p || !p.view.length) return;
  p.active = (p.active + delta + p.view.length) % p.view.length;
  Array.from(p.list.children).forEach((li, i) => li.classList.toggle("is-active", i === p.active));
  const el = p.list.children[p.active];
  if (el) el.scrollIntoView({ block: "nearest" });
}

function openLaunchModelPicker(level, btn) {
  if (_lbPop && _lbPop.level === level) { _closeLaunchModelPop(); return; }   // toggle
  _closeLaunchModelPop();
  const lead = level === "deep" ? "pesado" : "rápido";
  const prov = _effLevelProvider(level);
  const el = document.createElement("div");
  el.className = "lb-model-pop";
  el.innerHTML =
    `<div class="lbp-head">Modelo ${lead}` +
    (prov ? ` <span class="lbp-prov">${escapeHtml(prov)}</span>` : "") + `</div>` +
    `<input type="text" class="lbp-search" autocomplete="off" role="combobox" aria-autocomplete="list" ` +
      `placeholder="filtrar modelos… (id ou nome)" />` +
    `<ul class="lbp-list" role="listbox"></ul>` +
    `<div class="lbp-foot">` +
      `<button type="button" class="lbp-default">padrão do provedor</button>` +
      `<button type="button" class="lbp-adv" title="Escolher o provedor deste nível nas Configurações">provedor</button>` +
    `</div>`;
  const group = btn.closest(".lb-model") || btn.parentNode;
  group.appendChild(el);
  const input = el.querySelector(".lbp-search");
  const list = el.querySelector(".lbp-list");
  _lbPop = { el, level, prov, input, list, view: [], active: -1 };
  _lbRenderPopList();
  // Sem lista ao vivo pro provedor deste nível: tenta buscar (owner/BYOK/openrouter) e
  // re-renderiza quando chegar. claude-cli e afins já têm catálogo — nada a buscar.
  if (!(_liveModels[prov] && _liveModels[prov].length)) {
    const form = _readConfigForm();
    refreshModelsForProvider(prov, { apiKey: prov === form.provider ? form.apiKey : "", baseUrl: form.baseUrl })
      .then(() => { if (_lbPop && _lbPop.level === level) _lbRenderPopList(); });
  }
  input.addEventListener("input", _lbRenderPopList);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); _lbPopMove(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); _lbPopMove(-1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (_lbPop.active >= 0 && _lbPop.view[_lbPop.active]) _lbChooseModel(level, _lbPop.view[_lbPop.active].id);
      else if (input.value.trim()) _lbChooseModel(level, input.value.trim());   // texto livre (fallback)
    }
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("[data-val]");
    if (!li) return;
    e.preventDefault();
    _lbChooseModel(level, li.getAttribute("data-val"));
  });
  el.querySelector(".lbp-default").addEventListener("click", () => _lbChooseModel(level, ""));
  el.querySelector(".lbp-adv").addEventListener("click", () => { _closeLaunchModelPop(); openConfigForLevel(level); });
  document.addEventListener("mousedown", _lbPopOutside, true);
  document.addEventListener("keydown", _lbPopEsc, true);
  setTimeout(() => input.focus(), 0);
}

// Abre o config e rola até o PAR daquele nível (provedor + modelo). Depois da 017 não
// há mais "modo avançado" pra ligar: o provedor por nível é o layout primário — o link
// do popover só leva o usuário até o bloco certo.
function openConfigForLevel(level) {
  const panel = $("configPanel");
  if (panel) panel.classList.remove("hidden");
  if (!_modelItems.length) refreshModels();
  const block = $(level === "deep" ? "cfgLevelDeep" : "cfgLevelQuick") || panel;
  if (block && block.scrollIntoView) block.scrollIntoView({ behavior: "smooth", block: "center" });
}

// Ao abrir um ativo (resultado / comparação / run com erro), a barra passa a apontar
// pra ele: ticker preenchido, método = o método ABERTO, TF = o do veredito. Assim
// Analisar/↻ reanalisam sem redigitar e a preservação de método por TF (031/037/039)
// se mantém — trocar de TF não reseta o método aberto.
function syncLaunchBarToOpen() {
  if (_openTicker) {
    const t = $("ticker");
    // não sobrescreve enquanto o usuário digita no campo
    if (t && document.activeElement !== t) t.value = _openTicker;
  }
  _barMethod = normMethod(_openView);
  _barTf = (_timeframes && _timeframes.includes(_verdictTf)) ? _verdictTf : "1d";
  renderLaunchBar();
}

function bindLaunchBar() {
  const tfsEl = $("launchTfs");
  if (tfsEl && !tfsEl._bound) {
    tfsEl._bound = true;
    tfsEl.addEventListener("click", (e) => {
      const b = e.target.closest("button.lb-tf");
      if (b && !b.disabled) { _barTf = b.dataset.tf; renderLaunchBar(); }
    });
  }
  const mEl = $("launchMethods");
  if (mEl && !mEl._bound) {
    mEl._bound = true;
    mEl.addEventListener("click", (e) => {
      const b = e.target.closest("button.lb-method");
      if (b) { _barMethod = b.dataset.method; renderLaunchBar(); }
    });
  }
  // Seletor compacto de modelos (task 012): chip → popover pesquisável.
  const modelsEl = $("launchModels");
  if (modelsEl && !modelsEl._bound) {
    modelsEl._bound = true;
    modelsEl.addEventListener("click", (e) => {
      const b = e.target.closest("button.lb-model-pick");
      if (b) openLaunchModelPicker(b.dataset.level, b);
    });
  }
  const chip = $("dateChip");
  if (chip && !chip._bound) {
    chip._bound = true;
    chip.addEventListener("click", () => {
      const d = $("date");
      if (!d) return;
      // calendário nativo na hora; se showPicker não existir/for barrado, cai no foco
      if (d.showPicker) { try { d.showPicker(); return; } catch (e) { /* fallback abaixo */ } }
      d.focus();
    });
  }
  const dateInput = $("date");
  if (dateInput && !dateInput._bound) {
    dateInput._bound = true;
    dateInput.addEventListener("change", updateDateChip);
    dateInput.addEventListener("input", updateDateChip);
  }
  const rerun = $("rerunBtn");
  if (rerun && !rerun._bound) {
    rerun._bound = true;
    rerun.addEventListener("click", () => {
      if (!_openTicker) return;
      // ↻ = reanalisar o ABERTO hoje preservando o método aberto, no TF selecionado.
      runReanalyze(normMethod(_openView), _barTf);
    });
  }
}

// Dispara a reanálise do ativo ABERTO sem redigitar: método explícito + TF escolhido,
// dados de hoje (mesma semântica de "Atualizar"). Reusa /api/analyze (method|compare +
// timeframe) — o mesmo caminho do launcher, sem fluxo paralelo que possa divergir.
function runReanalyze(method, tf) {
  if (!_openTicker) return;
  const compare = method === "compare";
  // O método vai INTEIRO pro backend (normMethod), nunca achatado. Achatar aqui era o
  // bug: setup123 (atalho estrutural, $0 de LLM) caía em "padrao" e subia o pipeline
  // multi-agente completo — o botão prometia $0 e cobrava uma análise inteira.
  // 'compare' viaja na flag `compare`, e o método base da comparação é o padrão.
  const nm = normMethod(method);
  const m = nm === "compare" ? "padrao" : nm;
  const date = _todayManaus || _openDate || "";
  $("formError").textContent = "";
  $("resultPanel").classList.add("hidden");
  $("comparePanel").classList.add("hidden");
  $("steps").innerHTML = "";
  const boot = compare
    ? "Comparando Padrão × Erick…"
    : (m === "erick" ? "Método Erick — subindo o motor…"
      : m === "setup123" ? "Setup123 — leitura estrutural, sem LLM…"
      : m === "storm123" ? "Storm123 + Éden — leitura estrutural, sem LLM…"
      : "Subindo o motor…");
  renderProgress({
    status: "running", ticker: _openTicker, elapsed: 0, cost: null,
    progress: { phase: "Inicializando", label: boot, percent: 2, plan: [], reached: [] },
  });
  apiPost("/api/analyze", { ticker: _openTicker, date, method: m, compare, timeframe: tf || "1d" })
    .then((r) => r.json())
    .then((data) => {
      if (data && data.run_id) {
        rememberRunToken(data.run_id, data.run_token);
        watchRun(data.run_id); loadHistory();
      } else { $("formError").textContent = (data && data.error) || "falha ao reanalisar"; }
    })
    .catch(() => { $("formError").textContent = "falha ao reanalisar"; });
}

// R:R ABAIXO DE 1 é estado, não um número como outro qualquer: significa arriscar
// mais do que se pretende ganhar (0,31 = arrisca 3,2x o alvo). O gráfico já trata
// assim desde a task 029 (âmbar dentro do canvas quando rr < 1) — aqui a mesma
// gramática chega ao texto, que é o que se lê no celular antes de rolar até o
// gráfico. Âmbar de ATENÇÃO, não vermelho de erro: o setup existe, a conta é que
// está desfavorável.
function rrRuim(rr) {
  return rr != null && rr < 1;
}

// Quantas vezes o risco supera o retorno, em pt-BR: o resto da tela escreve
// "0,21:1" e "218,40", e o multiplicador saía "4.8x" — mesma frase, duas
// convenções de decimal.
function rrVezes(rr) {
  return (1 / rr).toFixed(1).replace(".", ",");
}

function rrAviso(rr) {
  if (!rrRuim(rr)) return "";
  return ` — risco MAIOR que o retorno: arrisca ${rrVezes(rr)}x o que pretende ganhar`;
}

// R:R ruim vira PALAVRA, não cor (DA-078 regra 3: âmbar saiu da paleta; aviso se
// resolve com palavra e hierarquia). Era o âmbar que dizia "atenção" — sem ele, e
// sem isto, 0,31 ficaria com a mesma cara de 2,50, que é informação sumindo da tela.
function rrMarca(rr) {
  return rrRuim(rr) ? `risco > retorno (${rrVezes(rr)}x) · ` : "";
}

// ---- rodapé do cabeçalho: gatilhos + preço, no canto inferior direito -------
// Pedido do Samyr: o VEREDITO fica em cima; os GATILHOS descem pro canto inferior
// direito do card, ao lado do preço atual, alinhados à direita. Antes os níveis só
// existiam no texto abaixo do gráfico — o leitor tinha que caçar o gatilho.
//
// E o PREÇO passa a dizer o que é. O plano é date-guarded: o número que ele carrega
// é o último FECHAMENTO da série (MSFT em 29/08 mostrava 505,06 de 27/08 com o papel
// valendo 513,53), e a tela o exibia como se fosse "agora". Agora a run de HOJE
// carrega a cotação com a SESSÃO declarada (fechamento × pré-market × after-market
// são preços diferentes) e o preço da análise fica ao lado, como referência.
// Cada preço é uma UNIDADE fechada — número · o que ele é · de quando — e as
// unidades não quebram por dentro: a tira quebra ENTRE elas, nunca no meio de uma e
// nunca cortando (era o defeito 1: a fila corria até a borda e a data sumia).
// O rótulo e o carimbo de hora deixam de ser o mesmo texto colado ("COTAÇÃO AGORA ·
// 24H · 29/08 20:42" era ilegível: não dava pra saber se o "24h" qualificava a
// cotação e se o horário era dela ou da análise). Agora o rótulo é rótulo (caixa
// alta, apagado) e a hora é hora (mono), cada uma dentro da SUA unidade.
function renderHeadPrice(a, live) {
  const el = $("headPrice");
  const box = $("headLevels");
  // O carimbo da análise MUDA de hora quando se troca o frame — "28/08" no diário,
  // "19:30" no 1h, "17:30" no 4h — porque é o ÚLTIMO CANDLE daquele frame. Faz
  // sentido e, sem rótulo, parecia dado inconsistente. Agora a unidade diz o que o
  // horário é, no mesmo molde da cotação (número · o que ele é · de quando).
  const candle = a && a.timeframe
    ? `último candle ${tfNome(_tf)}` : "último candle";
  const analise = a && a.price != null
    ? `<span class="hp-unit hp-ref"><span class="hp-k">análise</span>` +
      `<b>${fmtNum(a.price)}</b>` +
      `<span class="hp-tag">${escapeHtml(candle)}</span>` +
      // O as_of da análise costuma trazer a HORA do candle no intradiário; ela
      // estava sendo jogada fora por fmtDate e é justamente o que distingue este
      // momento do da cotação (os dois caem no mesmo dia).
      (a.as_of ? `<span class="hp-when">${fmtMomento(a.as_of)}</span>` : "") +
      `</span>`
    : "";
  // A cotação só vale como ATUAL no dia em que foi tirada: o resultado é persistido,
  // e uma run reaberta amanhã mostraria a de hoje como se fosse de agora.
  const hoje = _todayManaus || "";
  const atual = live && live.price != null && (!live.em || !hoje || live.em === hoje)
    ? `<span class="hp-unit hp-live"><b>${fmtNum(live.price)}</b>` +
      `<span class="hp-tag">${escapeHtml(live.rotulo || "cotação")}</span>` +
      (live.as_of ? `<span class="hp-when">${escapeHtml(live.as_of)}</span>` : "") +
      `</span>`
    : "";
  // DISTÂNCIA entre as duas (task 037): a cotação agora contra o preço em que a
  // análise foi lida decide se o plano ainda vale — o usuário não deveria ter que
  // subtrair de cabeça. Só existe quando as DUAS unidades acima existem de verdade
  // (senão não há o que distanciar) e o preço da análise não é zero.
  //
  // O gatilho é a unidade `atual` RENDERIZADA, não `live.price`: a cotação carrega
  // a guarda de dia (DA-073) e some quando a run é reaberta amanhã. Repetir só o
  // `live.price != null` aqui deixava a distância na tela contra um preço que a
  // tela não mostra mais — distância de quê? — e ainda empurrava a régua da
  // .hp-ref pra dentro de uma linha de uma unidade só (ela vive do :first-child).
  const diff = (atual && a && a.price != null && a.price)
    ? (() => {
        const d = live.price - a.price;
        const pct = (d / a.price) * 100;
        const cls = d > 0 ? "up" : (d < 0 ? "down" : "flat");
        // ↑ ↓ · — a MESMA gramática da variação do histórico (.pchg, `linhaPreco`),
        // e o que a DA-076 deixa ficar: seta de direção é tipográfica. O triângulo
        // cheio que estava aqui é pictograma geométrico e cai no portão de
        // varredura Unicode — que lê o arquivo INTEIRO, comentário incluído, então
        // nem citá-lo aqui dá.
        const arrow = d > 0 ? "↑" : (d < 0 ? "↓" : "·");
        const sign = d > 0 ? "+" : (d < 0 ? "−" : "");
        return `<span class="hp-unit hp-diff ${cls}"><span class="hp-k">distância</span>` +
          `<b>${arrow} ${sign}${fmtNum(Math.abs(d))} (${sign}${pctBR(Math.abs(pct))}%)</b></span>`;
      })()
    : "";
  if (!analise && !atual) {
    el.classList.add("hidden"); el.innerHTML = "";
  } else {
    // Sem separador de texto entre as unidades: quem separa é a régua da .hp-ref/
    // .hp-diff, que tem peso visual de verdade — o "análise" solto tinha o mesmo
    // peso do resto e por isso não separava nada, virava mais um item da lista
    // (defeito 4). A distância entra ENTRE agora e análise: é o que liga as duas.
    el.innerHTML = atual + diff + analise;
    el.classList.remove("hidden");
  }
  if (box) box.classList.toggle("hidden", !el.innerHTML);
}

// ---- UM CARD POR ANÁLISE ---------------------------------------------------
// A tela desenha DUAS leituras independentes sobre o mesmo candle, e elas podem
// coexistir e discordar: o PADRÃO 1-2-3 (gatilho na máxima do ponto 2) e o RECUO
// À MÉDIA (a faixa verde da média ascendente). A task 015 deu NOME a cada uma
// (``setup_source``) porque "Setup ativo agora" não dizia de quem falava. A 021 dá
// CAIXA: enquanto as duas dividiam um amontoado — uma tira de níveis no cabeçalho
// e um bloco de estado solto na coluna, sem vínculo visual entre eles —, a
// discordância entre elas lia como CONTRADIÇÃO DA TELA.
//
// A DIVISÃO SAI DO DADO, não de palpite. Cada campo do ``actionable`` tem um dono
// determinado por como ``price_structure.build_actionable_plan`` o produz:
//   • 1-2-3         → pattern, invalidation, stop, target, risk_reward — os cinco
//                     saem de ``_pattern_levels(struct.pattern, …)`` e são ``None``
//                     JUNTOS quando não há padrão;
//   • recuo à média → buy_zone (e o pullback_zone que é recuo), de
//                     ``struct.active_region`` / ``struct.buy_regions``;
//   • de NINGUÉM    → price, as_of, timeframe e a cotação: o chão comum contra o
//                     qual as duas se medem (cabeçalho em cima, frame no rodapé);
//   • setup_state + horizon → vão pro card que ``setup_source`` NOMEAR. É
//                     literalmente pra isso que o campo existe. Sem dono (o
//                     backend pode eleger nenhuma), o carimbo cai no rodapé
//                     compartilhado em vez de ser enfiado num card que não o gerou.
//
// Consequências que a divisão obriga:
//   • nenhum número aparece nos dois cards, e o R:R — que saía DUAS VEZES, na tira
//     do cabeçalho e no bloco do setup — passa a sair uma só, no 1-2-3, que é de
//     onde ele vem (entrada = gatilho, risco = stop, retorno = alvo);
//   • as BASES dos níveis ("invalidação + folga de 0.5·ATR14", "topo anterior …")
//     desceram da nota do gráfico pra cá, ao lado do número que elas justificam —
//     a nota parou de listar os mesmos preços que o canvas já pinta (ver
//     renderChartCard);
//   • card que não tem dado não abre: sem padrão não há card de 1-2-3, sem faixa
//     não há card de recuo. Nunca uma caixa vazia com travessão inventado.
// SEM PICTOGRAMA (DA-076: "tira todos os emojis" — o estado volta a ser cor e
// palavra). Nesta superfície nova a regra já entra valendo: a leitura se identifica
// pela COR da borda (a mesma do gráfico: azul/laranja no 1-2-3 por direção, verde no
// recuo) e pelo NOME escrito por extenso. O ícone que estava aqui não dizia nada que
// o rótulo não diga — e com quatro leituras na mesma coluna (Storm, tasks 022/023)
// vira mosaico.

// "3.5" -> "3,5% acima" — a distância do preço até a média é o único número desta
// leitura que o gráfico NÃO desenha (ele desenha a faixa, não o quanto falta).
function fmtDist(pct) {
  const s = Math.abs(pct).toFixed(1).replace(".", ",");
  return `${s}% ${pct >= 0 ? "acima" : "abaixo"}`;
}

// Uma linha do card: nome · NÚMERO · a base que o justifica. A base é o que
// transforma "stop (SL) 764,76" em nível defensável; ela morava na nota do gráfico,
// longe do número, e agora mora colada nele.
function scRow(nome, valor, base, cls, titulo) {
  return `<div class="sc-row${cls ? " " + cls : ""}"` +
    (titulo ? ` title="${escapeHtml(titulo)}"` : "") + ">" +
    `<span class="sc-k">${escapeHtml(nome)}</span>` +
    `<b class="sc-v">${escapeHtml(valor)}</b>` +
    (base ? `<span class="sc-basis">${escapeHtml(base)}</span>` : "") +
    "</div>";
}

// Nível que a análise não produziu: diz "sem nível definido" em vez de inventar
// número (mesma regra que a nota do gráfico aplicava antes de perder essas linhas).
// Nível que NÃO EXISTE naquela leitura é outra coisa: nem linha ele ganha.
function scSemNivel(nome, valor) {
  return `<div class="sc-row sc-sem"><span class="sc-k">${escapeHtml(nome)}</span>` +
    `<span class="sc-v sc-sem-v">${escapeHtml(valor || "sem nível definido")}</span></div>`;
}

// ============ NÍVEL RECUSADO SE EXPLICA ONDE SE PROCURA (DA-123) ==============
//
// "kd o alvo do Setup123?" — o Samyr, olhando o gráfico do MSFT no 4h. O alvo
// estava AUSENTE por decisão correta (o papel está na máxima da série: não há topo
// anterior acima da entrada, então não há retorno a projetar, e a task 008
// estabeleceu que número inventado não se publica). O defeito era de COMUNICAÇÃO:
// na legenda apareciam "recuo à média", "invalidação" e "stop (SL)", e o alvo
// simplesmente não existia — sem uma palavra. O único vestígio era um
// "R:R não calculável" num canto, que não responde "cadê o alvo".
//
// O MOTIVO já existia no dado (``risk_reward.note``, escrito pelo backend). Estas
// funções o levam até onde a pergunta nasce, e NENHUMA delas escreve texto novo:
// uma devolve a frase inteira, a outra a PRIMEIRA ORAÇÃO dela, para caber onde o
// espaço é de uma linha. Inventar prosa aqui criaria uma segunda explicação para o
// mesmo fato, e as duas divergiriam no dia em que o backend mudasse a dele.

// A primeira oração do motivo — um EXTRATO, nunca um texto paralelo.
function motivoCurto(nota) {
  const t = String(nota || "").trim();
  if (!t) return "";
  const corte = t.search(/\s—\s|;|\.\s|\.$/);
  return (corte > 0 ? t.slice(0, corte) : t).trim();
}

// Os níveis que a LEITURA recusou publicar, com o motivo. Genérico de propósito:
// a regra é "ausência declarada" (task 008), e hoje só o alvo cai nela — mas o dia
// em que o stop ou a invalidação forem recusados, a legenda já sabe dizer.
function niveisRecusados(a) {
  if (!a) return [];
  const out = [];
  const rr = a.risk_reward || {};
  const temPadrao = !!a.pattern;
  const semAlvo = !(a.target && a.target.price != null);
  if (temPadrao && semAlvo && rr.note) out.push({ nome: "alvo (TP)", motivo: rr.note });
  return out;
}

// A LINHA DO R:R NUNCA SOME. Ela é o número que diz se o setup vale o risco, e
// some-la quando não dá pra calcular deixava o leitor sem saber se o R:R era
// ruim, bom ou inexistente — nos prints do Samyr (mesmo ativo, três frames) ele
// aparecia só no diário, e no 1h e no 4h não havia nem a linha nem uma palavra.
// Sem número, a linha carrega o MOTIVO, que o backend escreve.
//
// E QUANDO O PADRÃO JÁ ACIONOU SÃO DOIS NÚMEROS, não um. Depois do rompimento a
// entrada de referência passa a ser o preço atual (é o que resta de trade), mas o
// stop continua no ponto 3 — então o R:R desaba conforme o trade AMADURECE. Um
// 0,09:1 sozinho na tela lê-se como "o método dá trade ruim"; ao lado do 5,97:1
// que o setup oferecia NO GATILHO, lê-se como "cheguei tarde", que é a verdade.
// Sem os dois, o leitor tira a conclusão errada sobre o método.
function rrLinha(rr, entradaTxt) {
  const linhas = [];
  const temGatilho = !!(rr && rr.no_gatilho && rr.no_gatilho.rr != null);
  const nome = temGatilho ? "risco/retorno agora" : "risco/retorno";
  if (rr && rr.rr != null) {
    linhas.push(scRow(nome, `${fmtNum(rr.rr)}:1`,
      `${rrMarca(rr.rr)}${entradaTxt} · risco ${fmtNum(rr.risk)} · retorno ${fmtNum(rr.reward)}`,
      rrRuim(rr.rr) ? "rr-ruim" : "", rrRuim(rr.rr) ? "R:R" + rrAviso(rr.rr) : ""));
  } else {
    const motivo = (rr && rr.note) || "sem base: esta leitura não produziu stop nem alvo.";
    linhas.push(`<div class="sc-row sc-sem"><span class="sc-k">${escapeHtml(nome)}</span>` +
      `<span class="sc-v sc-sem-v">não calculável</span>` +
      `<span class="sc-basis">${escapeHtml(motivo)}</span></div>`);
  }
  if (temGatilho) {
    const g = rr.no_gatilho;
    linhas.push(scRow("no gatilho", `${fmtNum(g.rr)}:1`,
      `era o que o setup oferecia a quem entrou em ${fmtNum(g.entry)} · ` +
      `risco ${fmtNum(g.risk)} · retorno ${fmtNum(g.reward)}`, "sc-retro"));
  }
  // O PERCURSO é a régua que explica a queda — medida, não faixa arbitrária.
  if (rr && rr.andado_pct != null) {
    linhas.push(scRow("percurso do setup",
      `andou ${fmtPct0(rr.andado_pct)} · sobra ${fmtPct0(rr.sobra_pct)}`,
      rr.motivo || "", "sc-percurso"));
  } else if (rr && rr.motivo) {
    linhas.push(`<div class="sc-row sc-warn">${escapeHtml(rr.motivo)}</div>`);
  }
  return linhas.join("");
}

// Percentual inteiro em pt-BR ("91%"). Aceita negativo e acima de 100 — os dois
// são fatos do percurso (preço voltou atrás do gatilho / alvo já batido).
function fmtPct0(v) {
  return v == null ? "—" : `${Math.round(v)}%`;
}

// Rótulo pt-BR da qualidade do Storm. "neutra" é o terceiro estado do Éden (task
// 016): o candle entre a MME 8 e a MME 80 — a ZONA NEUTRA que o Stormer nomeia.
// Ela OPERA, mas vale menos: não é veto, é aviso, e o card escreve o porquê.
const STORM_QUALIDADE = {
  perfeita: "perfeita", boa: "boa", neutra: "zona neutra", ruim: "ruim",
};
// Nome curto de cada ENTRADA do Storm. São DUAS leituras do MESMO padrão (mesmos
// pontos, mesmo stop, mesma amplitude), com gatilhos diferentes — a spec escreve
// "rompimento da máxima do ponto 2 (ou 3)". Colapsar as duas num número só é
// esconder justamente a que entra antes.
const STORM_ENTRADA = {
  ponto2: "entrada no ponto 2", ponto3: "entrada no ponto 3",
  ponto2e3: "entrada nos pontos 2 e 3 (mesmo nível)",
};
const STORM_ORDEM_CURTO = { antecipada: "antecipada", confirmada: "confirmada", unica: "única" };

// O card do 1-2-3 STORM. Separado numa função porque ele carrega duas coisas que
// nenhuma das outras leituras tem: um FILTRO COM PODER DE VETO (o Éden) e DUAS
// ENTRADAS do mesmo padrão. O veto é a manchete do card, não uma nota de rodapé —
// e a borda muda de cor com ele, que é a gramática que a DA-076 pede (estado é cor
// + palavra).
// O NOME DO ÉDEN NA TELA — lido, nunca inventado.
//
// *"nos cards de texto onde usamos Éden, identifica Éden de Alta e de Baixa na
// menção."* O vocabulário mora num lugar só, no produtor (`price_structure._EDEN_ROTULO`),
// e viaja pronto no payload: `rotulo` pra leitura, `rotulo_curto` pro espaço apertado.
// Aqui não há tabela nenhuma — se houvesse, seriam DOIS vocabulários, que é como a tela
// ganhou três jeitos de dizer timeframe (DA-095).
//
// Sem rótulo no payload (run ANTIGA, salva antes desta task) devolve vazio, e cada
// chamador degrada pro texto que aquela run sempre mostrou. Inventar o nome aqui seria
// afirmar sobre um cálculo que não o produziu.
function edenNome(eden) {
  return (eden && eden.rotulo) || "";
}

function edenCurto(eden) {
  return (eden && (eden.rotulo_curto || eden.rotulo)) || "";
}

// A equivalência com a doutrina do Stormer vai no `title`: quem leu "Éden de compra" no
// material precisa reconhecer o "Éden de Alta" da tela.
function edenAjuda(eden) {
  const d = eden && eden.doutrina;
  return d ? `${edenNome(eden)} — na doutrina do Stormer, ${d}` : edenNome(eden);
}

// O CONTRASTE do Éden ALINHADO na direção CONTRÁRIA ao padrão vetado — só esse caso:
// "Éden de Alta" sozinho, num rótulo de veto, lê como se ELE fosse o defeito, e é o
// oposto: é regime bom, só que contra a direção deste padrão específico. Alinhado na
// MESMA direção do padrão não devia estar vetado por este motivo (é outro veto, tipo
// zona neutra) — mas por segurança só troca quando as direções de fato divergem, nunca
// por só ver `alinhado`. "sem Éden" e "armadilha" já soam ruins por conta própria (não
// entram aqui). Troca "Éden de " pela direção — MESMO TAMANHO de texto (a pílula do
// gráfico é a largura de uma vela, sem sobra pra crescer), então quem já cabia
// continua cabendo.
function edenContraste(eden, direction, nome) {
  if (!eden || !eden.alinhado || !direction) return nome;
  if (eden.direcao === direction) return nome;
  if (!nome.includes("Éden de ")) return nome;
  return nome.replace("Éden de ", `${direction} × `);
}

function stormCardHtml(st, frameDoBloco) {
  const pat = st.pattern;
  const frameProprio = (st.timeframe && frameDoBloco && st.timeframe !== frameDoBloco)
    ? st.timeframe : "";
  const eden = st.eden || {};
  const opera = st.opera === true;
  const dir = pat ? ((PAT_DIR[pat.direction] || [])[1] || "") : "";
  const rows = [];

  // ÉDEN primeiro: é ele que autoriza ou proíbe. Sem as duas médias na linha, o
  // leitor não tem como conferir o veto — e um veto que não se confere é palpite.
  // A CHAVE DA LINHA É O ESTADO DO ÉDEN, não o nome das médias. "MME 8 × MME 80" com
  // dois números dizia COMO o filtro é medido e nunca QUAL era o resultado — e o
  // resultado é a única coisa que decide se o setup opera. As duas médias não se
  // perdem: continuam no valor (é com elas que se confere o veto) e na base.
  if (eden.disponivel) {
    rows.push(scRow(edenNome(eden) || `MME ${8} × MME ${80}`,
      `${fmtNum(eden.ema_rapida)} × ${fmtNum(eden.ema_lenta)}`,
      `MME 8 × MME 80 — ${eden.motivo || ""}`, "", edenAjuda(eden)));
  } else {
    rows.push(`<div class="sc-row sc-sem" title="${escapeHtml(edenAjuda(eden))}">` +
      `<span class="sc-k">${escapeHtml(edenNome(eden) || "Éden (MME 8 × MME 80)")}</span>` +
      `<span class="sc-v sc-sem-v">indisponível</span>` +
      `<span class="sc-basis">${escapeHtml(eden.motivo || "")}</span></div>`);
  }

  if (pat && ehFantasma(pat)) {
    const inv0 = st.invalidation || {};
    const quando = pat.invalidado_em ? ` em ${fmtDate(pat.invalidado_em)}` : "";
    rows.push(scRow("INVALIDADO", `perdeu ${fmtNum(inv0.price != null ? inv0.price : pat.p2.price)}${quando}`,
      (pat.direction === "venda"
        ? "o preço FECHOU acima do ponto 2: o topo que a reversão declarou foi desfeito e este Storm não existe mais."
        : "o preço FECHOU abaixo do ponto 2: o fundo que a reversão declarou foi desfeito e este Storm não existe mais.")
      + " Os gatilhos abaixo são história — uma nova entrada exige um novo padrão de três candles.",
      "sc-morto"));
  }
  if (pat) {
    // Invalidação e stop são COMUNS às duas entradas — mesmo padrão, mesmo ponto 2 —,
    // então ficam FORA das leituras, uma vez só. E no Storm eles são o MESMO nível
    // (stop sem folga), o que rende uma linha, não duas com o mesmo número.
    const inv = st.invalidation || {};
    const sl = st.stop || {};
    const mesmoNivel = inv.price != null && sl.price != null && inv.price === sl.price;
    if (mesmoNivel) {
      rows.push(scRow("stop (SL) = invalidação (ponto 2)", fmtNum(sl.price),
        `${inv.meaning || ""} · ${sl.basis || ""}`));
    } else {
      if (inv.price != null) {
        rows.push(scRow("invalidação (ponto 2)", fmtNum(inv.price), inv.meaning || ""));
      }
      if (sl.price != null) rows.push(scRow("stop (SL)", fmtNum(sl.price), sl.basis || ""));
    }
    // AS DUAS LEITURAS, lado a lado e nomeadas. A ANTECIPADA vem primeiro porque é
    // o gatilho que o preço alcança antes — é a ordem em que os eventos acontecem,
    // não uma preferência. Cada uma leva o SEU gatilho, o SEU alvo e o SEU R:R; o
    // stop é o mesmo lá em cima, e é por isso que o R:R difere.
    const leituras = (st.leituras || []).slice().sort(
      (x, y) => (x.ordem === "confirmada" ? 1 : 0) - (y.ordem === "confirmada" ? 1 : 0));
    leituras.forEach((L) => {
      const nome = STORM_ENTRADA[L.entrada] || L.entrada;
      const ordem = STORM_ORDEM_CURTO[L.ordem] || L.ordem || "";
      rows.push(`<div class="sc-leitura" title="${escapeHtml(L.ordem_label || "")}">` +
        `<span class="sc-lk">${escapeHtml(nome)}</span>` +
        (ordem ? `<span class="sc-lo">${escapeHtml(ordem)}</span>` : "") +
        `<span class="sc-lstate">${escapeHtml(L.state_label || L.state || "")}</span></div>`);
      if (L.trigger != null) rows.push(scRow("gatilho", fmtNum(L.trigger), L.label || ""));
      const tp = L.target || {};
      if (tp.price != null) rows.push(scRow("alvo (TP)", fmtNum(tp.price), tp.label || ""));
      rows.push(rrLinha(L.risk_reward || {}, (L.risk_reward || {}).entry_basis || ""));
    });
  } else {
    rows.push(`<div class="sc-row sc-sem-txt">Nenhum 1-2-3 Storm na janela lida ` +
      `(três candles: alta/lateral, fundo, recuperação que falha em romper o ponto 1).</div>`);
  }
  // A preparação do Storm é de OUTRA natureza: o ponto 3 é o PRÓXIMO candle, não um
  // swing futuro qualquer. A condição vem escrita do backend, com a regra dele.
  const pjSt = st.projecao_p3;
  if (pjSt && pjSt.low != null) {
    rows.push(scRow("preparação — ponto 3", `${fmtNum(pjSt.low)}–${fmtNum(pjSt.high)}`,
                    pjSt.condicao || "", "sc-prep"));
  }

  // A manchete: OPERA / NÃO OPERA + a qualidade, e o motivo escrito embaixo.
  const q = STORM_QUALIDADE[st.qualidade] || st.qualidade || "";
  // A ZONA NEUTRA opera, mas NÃO se veste de "opera" limpo: ela tem estado próprio
  // na tela, porque "opera · qualidade zona neutra" lido rápido vira só "opera" —
  // e o aviso do Stormer ("muito mais perigoso") sumiria na leitura de relance.
  const neutra = st.qualidade === "neutra";
  const selo = pat
    ? `<div class="sc-verdict" title="${escapeHtml(edenAjuda(eden))}">` +
      `<span class="sc-vk">${escapeHtml(edenNome(eden) || "filtro Éden")}</span>` +
      `<span class="sc-state ${neutra ? "aguardar_pullback" : (opera ? "ativo" : "sem_setup")}">` +
      `${neutra ? "OPERA COM CAUTELA" : (opera ? "opera" : "NÃO OPERA")}` +
      `${q ? ` · qualidade ${escapeHtml(q)}` : ""}</span></div>` +
      (st.veto ? `<div class="sc-veto">${escapeHtml(st.veto)}</div>`
               : (st.motivo ? `<div class="sc-hz">${escapeHtml(st.motivo)}</div>` : ""))
    : "";
  const mortoSt = ehFantasma(pat);
  return `<section class="setup-card sc-storm${mortoSt ? " sc-fantasma" : ""}${opera ? "" : " sc-vetado"}` +
    `${pat && pat.direction === "venda" ? " sc-venda" : ""}">` +
    `<div class="sc-head"><span class="sc-title">Storm123` +
    (dir ? ` <span class="sc-dir">${escapeHtml(dir)}</span>` : "") + "</span>" +
    // Frame PRÓPRIO só quando difere do bloco: aí ele salta, porque um card lido
    // sob o carimbo errado é um stop lido no frame errado. Igual ao bloco, cala.
    (frameProprio ? `<span class="sc-frame-card">${escapeHtml(frameProprio)}</span>` : "") +
    "</div>" + selo +
    `<div class="sc-rows">${rows.join("")}</div></section>`;
}

function renderSetupCards(a) {
  const el = $("setupCards");
  if (!el) return;
  if (!a || !a.setup_state) { el.classList.add("hidden"); el.innerHTML = ""; return; }

  // Carimbo do VEREDITO — estado + horizonte — no card da leitura que o produziu.
  // É o que torna a discordância LEGÍVEL: dá pra ver qual das duas decidiu, em vez
  // de um estado órfão pairando sobre as duas.
  const vlabel = SETUP_PT[a.setup_state] || a.setup_state;
  // A FASE vem PRIMEIRO e o mecanismo em seguida (DA-121). É a tradução explícita
  // entre as taxonomias, e ela mora na TELA: quem vê "AGUARDANDO · recuo à média"
  // aqui e "AGUARDANDO" no scan não precisa deduzir que são o mesmo momento.
  const fase = faseDoSetupState(a.setup_state);
  const mec = mecanismoPt(a.setup_state);
  const faseChip = fase
    ? `<span class="sc-fase ${escapeHtml(fase)}" title="${escapeHtml(faseAjuda(fase))}">` +
      `${escapeHtml(faseRotulo(fase))}</span>` +
      (mec ? `<span class="sc-mec">${escapeHtml(mec)}</span>` : "")
    : "";
  const carimbo =
    `<div class="sc-verdict"><span class="sc-vk">veredito do plano</span>` + faseChip +
    `<span class="sc-state ${escapeHtml(a.setup_state)}">${escapeHtml(vlabel)}</span>` +
    (a.horizon ? `<span class="sc-hz">horizonte: ${escapeHtml(a.horizon)}</span>` : "") + "</div>";
  const dono = a.setup_source ? String(a.setup_source) : "";
  const cards = [];

  // ---- card do 1-2-3 STORM (existe quando a run é do método Storm) ----------
  // Terceira leitura independente, e a única com VETO: o Éden dos Traders (MME 8 ×
  // MME 80) decide se o setup opera. Vetado, o card DIZ que não opera e por quê —
  // nunca um setup silenciosamente rebaixado. Os níveis continuam à vista porque
  // "por que não opera" precisa do que ele seria.
  const st = a.storm;
  if (st) {
    cards.push(stormCardHtml(st, a.timeframe));
  }

  // ---- card do PADRÃO 1-2-3 (existe quando existe padrão) -------------------
  // Leva o conjunto COMPLETO dos níveis DELE: gatilho (e a entrada, quando ela não
  // é o gatilho) · invalidação · stop · alvo · R:R. Não é duplicata do outro card —
  // são análises diferentes, com números diferentes por construção.
  const pat = a.pattern;
  if (pat) {
    const pdir = (PAT_DIR[pat.direction] || [])[1] || "";
    // declarado AQUI, no topo do bloco: o detalhe da morte é a primeira linha do
    // card, e uma const usada antes da declaração é ReferenceError em tempo de
    // execução — o card inteiro sumiria em vez de sair sem uma linha
    const morto = ehFantasma(pat);
    const rr = a.risk_reward || {};
    const rows = [];
    // O DETALHE DA MORTE vem PRIMEIRO — antes dos níveis, porque muda o sentido de
    // todos eles. Qual nível foi perdido, QUANDO, e o que significa pra quem estava
    // posicionado: um selo "invalidado" sozinho não deixa conferir nada.
    if (morto) {
      const inv0 = a.invalidation || {};
      const quando = pat.invalidado_em ? ` em ${fmtDate(pat.invalidado_em)}` : "";
      rows.push(scRow("INVALIDADO", `perdeu ${fmtNum(inv0.price != null ? inv0.price : pat.p3.price)}${quando}`,
        (pat.direction === "venda"
          ? "o preço FECHOU acima do ponto 3: os topos deixaram de ser descendentes e este 1-2-3 de venda não existe mais."
          : "o preço FECHOU abaixo do ponto 3: os fundos deixaram de ser ascendentes e este 1-2-3 de compra não existe mais.")
        + " Quem estava posicionado por ele perdeu a premissa — o gatilho deste padrão não vale mais, e uma nova entrada exige um novo ponto 3.",
        "sc-morto"));
    }
    if (pat.trigger != null) {
      rows.push(scRow("gatilho", fmtNum(pat.trigger),
        pat.direction === "venda" ? "perda da mínima do ponto 2"
                                  : "rompimento da máxima do ponto 2"));
    }
    // Entrada SÓ vira linha quando difere do gatilho (padrão já acionado entra a
    // preço de mercado). Igual ao gatilho, repeti-la seria o mesmo número duas
    // vezes no mesmo card — que é precisamente o que não se faz.
    const entradaPropria = rr.entry != null && rr.entry !== pat.trigger;
    if (entradaPropria) rows.push(scRow("entrada", fmtNum(rr.entry), rr.entry_basis || ""));
    const inv = a.invalidation || {};
    rows.push(inv.price != null
      ? scRow("invalidação", fmtNum(inv.price), inv.meaning || inv.label || "")
      : scSemNivel("invalidação"));
    const sl = a.stop || {};
    rows.push(sl.price != null
      ? scRow("stop (SL)", fmtNum(sl.price), sl.basis || "")
      : scSemNivel("stop (SL)"));
    const tp = a.target || {};
    // Alvo recusado não vira número sem sentido (DA-072 — alvo incoerente não se
    // publica). O MOTIVO, porém, desceu pra linha do R:R: é o ``note`` DELE, e
    // escrevê-lo aqui em cima fazia a linha de risco/retorno desaparecer junto —
    // duas informações no lugar de uma.
    if (tp.price != null && !(rr.note && rr.rr == null)) {
      rows.push(scRow("alvo (TP)", fmtNum(tp.price),
        (tp.label || "") + (tp.same_as_realize ? " (é o mesmo nível da região de realização)" : "")));
    } else if (tp.price != null) {
      rows.push(scSemNivel("alvo (TP)", "não publicável"));
    } else {
      // O MOTIVO CURTO na própria linha do alvo (DA-123). Ele era só "sem nível
      // definido", e a explicação ficava duas linhas abaixo, na do R:R — onde o
      // leitor que procura o ALVO não olha. Não é duplicata (DA-077): aqui vai a
      // primeira oração, lá vai a frase inteira; é o mesmo texto em dois níveis de
      // detalhe, como o card já faz com "no gatilho" e "percurso".
      rows.push(scSemNivel("alvo (TP)", motivoCurto(rr.note) || "sem nível definido"));
    }
    // A conta do R:R declara a entrada — mas quando ela É o gatilho (o caso comum),
    // repetir o número seria escrever o mesmo preço duas vezes no mesmo card: aqui
    // ele vira NOME ("entrada no gatilho").
    rows.push(rrLinha(rr, entradaPropria ? `entrada ${fmtNum(rr.entry)}` : "entrada no gatilho"));
    // O estado NATIVO do padrão fica sempre: "em formação" e "rompeu e retraçou
    // (não confirmado)" são fatos que o veredito não carrega — ele diz o que fazer,
    // não em que pé o padrão está.
    // O estado nativo do padrão ganha o PERCURSO junto: "acionado" sozinho não
    // distingue um rompimento de ontem de um trade que já andou 91% do caminho, e
    // são coisas diferentes pra quem vai entrar agora.
    const andado = rr.andado_pct;
    // INVALIDADO manda no rótulo de estado: "em formação" num padrão morto é a tela
    // descrevendo um setup que não existe mais. O estado nativo continua ao lado —
    // ele é a história, e é ela que explica onde o preço está.
    const pstate = morto
      ? `invalidado · era ${PAT_STATE[pat.state] || pat.state || ""}`
      : (PAT_STATE[pat.state] || pat.state || "")
        + (andado != null ? ` · andou ${fmtPct0(andado)} do caminho` : "");
    // A PREPARAÇÃO: onde o ponto 3 precisa nascer pra existir setup. A condição vem
    // escrita do backend — é a regra do detector, não uma frase que a tela inventa.
    const pj0 = a.projecao_p3;
    if (pj0) {
      rows.push(pj0.low != null
        ? scRow("preparação — ponto 3", `${fmtNum(pj0.low)}–${fmtNum(pj0.high)}`,
                pj0.condicao || "", "sc-prep")
        : `<div class="sc-row sc-sem"><span class="sc-k">preparação — ponto 3</span>` +
          `<span class="sc-v sc-sem-v">sem faixa a marcar</span>` +
          `<span class="sc-basis">${escapeHtml(pj0.motivo || "")}</span></div>`);
    }
    cards.push(
      `<section class="setup-card sc-123${morto ? " sc-fantasma" : ""}${pat.direction === "venda" ? " sc-venda" : ""}">` +
      `<div class="sc-head"><span class="sc-title">Setup123` +
      (pdir ? ` <span class="sc-dir">${escapeHtml(pdir)}</span>` : "") + "</span>" +
      (pstate ? `<span class="sc-now">${escapeHtml(pstate)}</span>` : "") +
      "</div>" +
      (dono === "123" ? carimbo : "") +
      `<div class="sc-rows">${rows.join("")}</div></section>`);
  }

  // ---- card do RECUO À MÉDIA (existe quando existe a faixa) -----------------
  // Os níveis DESTA leitura são outros: a entrada é a faixa da média (não um
  // rompimento) e o alvo é a região de realização acima. Stop e invalidação não
  // existem aqui — e nível que não existe SOME; herdar o do 1-2-3 seria pior que
  // omitir, porque daria ao leitor um stop que esta leitura não calculou.
  const bz = a.buy_zone;
  if (bz && bz.price != null) {
    const ma = bz.ma_label || "média";
    const dentro = bz.active_now === true;
    const rows = [];
    const faixa = (bz.low != null && bz.high != null)
      ? `faixa ${fmtNum(bz.low)}–${fmtNum(bz.high)}${bz.band_basis ? ` (${bz.band_basis})` : ""}`
      : (bz.band_basis || "");
    rows.push(scRow(`entrada na ${ma}`, fmtNum(bz.price), faixa));
    if (bz.distance_pct != null) {
      rows.push(scRow("distância do preço", fmtDist(bz.distance_pct),
        dentro ? "dentro da faixa desenhada no gráfico"
               : "fora da faixa desenhada — não é entrada agora"));
    }
    // Região de realização: é o alvo DESTA leitura. Quando ela é o próprio gatilho
    // do 1-2-3 (``role: gatilho``), o número já é do outro card — e aí não sai aqui.
    const rz = a.realize_zone;
    if (rz && rz.price != null && rz.role !== "gatilho") {
      const tp = a.target || {};
      const mesmo = tp.price === rz.price && tp.same_as_realize;
      rows.push(scRow(rz.role_label || "realização (alvo)", fmtNum(rz.price),
        mesmo ? "as duas leituras convergem neste nível: é também o alvo do 1-2-3"
              : (rz.label || "")));
    }
    // Faixa de compra cobrindo a de realização é setup degenerado, e o backend
    // carimba isso; some da tela seria esconder o defeito, não corrigi-lo.
    if (bz.overlap_note) rows.push(`<div class="sc-row sc-warn">${escapeHtml(bz.overlap_note)}</div>`);
    cards.push(
      `<section class="setup-card sc-recuo">` +
      `<div class="sc-head"><span class="sc-title">Recuo à média` +
      ` <span class="sc-dir">${escapeHtml(ma)}</span></span>` +
      `<span class="sc-now">${dentro ? "preço na faixa" : "preço fora da faixa"}</span>` +
      "</div>" +
      (dono === "recuo_media" ? carimbo : "") +
      `<div class="sc-rows">${rows.join("")}</div>` +
      "</section>");
  }

  // Veredito ÓRFÃO: o backend pode não eleger nenhuma das duas (``setup_source``
  // nulo — é o caso do 1-2-3 já acionado sem média ativa). Enfiá-lo num card seria
  // atribuir a uma leitura um estado que ela não produziu; ele fica no rodapé, que
  // é o lugar do que não pertence a ninguém.
  const semCard = cards.length === 0;
  const donoNaTela = (dono === "123" && !!pat)
    || (dono === "recuo_media" && !!(bz && bz.price != null));
  if (semCard) {
    // Sem nenhuma das duas leituras, o rodapé É o card: um só, dizendo o que o
    // plano concluiu e por que não há nível — nunca dois cards vazios com "—".
    cards.push('<section class="setup-card sc-nenhum">'
      + '<div class="sc-head"><span class="sc-title">Sem leitura de preço</span></div>'
      + carimbo
      + '<div class="sc-rows"><div class="sc-row sc-sem-txt">Nem o padrão 1-2-3 nem o '
      + 'recuo à média produziram nível neste frame.</div></div></section>');
  }
  // O FRAME SOBE PRO TOPO DO BLOCO. Ele era o chão comum, escrito UMA vez — mas no
  // rodapé, em cinza, DEPOIS dos três cards. Quem lia o card do meio não sabia em que
  // frame aquele stop valia sem rolar até o fim, e o rodapé é o menor peso visual da
  // tela justamente para a informação que qualifica todo o resto.
  //
  // Um carimbo por card seria a mesma frase três vezes num celular; um carimbo no
  // TOPO, grudado (sticky), acompanha a rolagem e vale pros três. É a hierarquia que
  // o pedido pede: uma vez, mas onde não sai da vista.
  //
  // "as leituras", sem contar: um card pode carregar MAIS de uma leitura dentro (o
  // Storm tem duas entradas), então contar cards diria um número que não é o de
  // leituras — e um número errado é pior que nenhum.
  // O NOME sai do vocabulário único (`tfNome`), não da prosa do backend: o bloco
  // dizia "diário (referência) · semanal (tendência de fundo)" enquanto o gráfico
  // ao lado dizia "Diário". A prosa não se perde — ela é CONTEÚDO (o diário lê o
  // semanal como fundo), não o nome do frame, e vai pro title.
  const frameTopo = _tf
    ? `<div class="sc-frame-topo"${a.timeframe ? ` title="${escapeHtml(a.timeframe)}"` : ""}>` +
      `<span class="sc-frame-k">${cards.length > 1 ? "as leituras" : "leitura"} no</span>` +
      `<b class="sc-frame-v">${escapeHtml(tfNome(_tf))}</b></div>`
    : "";
  const rodape = (!semCard && !donoNaTela) ? carimbo : "";

  // LEITURA EXPLORATÓRIA — o frame na tela não é o que decidiu. Trocar o chip de
  // tempo recalcula o plano inteiro, e os planos discordam de verdade (mesma ação,
  // 29/08: SL 207,00 no 1h, 176,83 no 4h, 175,09 no diário). Sem isto os três eram
  // pintados com o mesmo peso, e três trades diferentes com a mesma cara é convite
  // a operar o errado. Nada some — os níveis continuam todos lá, inteiros; o que
  // muda é a tarja dizendo que não são o plano da decisão.
  const explor = ehExploratorio(_tf);
  const aviso = explor
    ? `<div class="sc-explor"><span class="sc-explor-k">exploratório</span>` +
      `<span>estes níveis são recalculados no ${escapeHtml(tfNome(_tf))} e ` +
      `NÃO são o plano da decisão — o veredito desta análise é no ` +
      `${escapeHtml(tfNome(_verdictTf))}.</span></div>`
    : "";
  el.classList.toggle("is-exploratorio", explor);
  el.innerHTML = frameTopo + aviso + cards.join("") +
    (rodape ? `<div class="sc-foot">${rodape}</div>` : "");
  el.classList.remove("hidden");
}

// ─────────────────── A ESCADA: OS CINCO FRAMES DE UMA VEZ ───────────────────
//
// *"Preciso que a análise do Storm123 e Setup123 seja mais ampla e na análise
// inicial já faça os timeframes de 15m, 1h, 4h, D e S."* É como o método funciona:
// o frame MAIOR manda na TESE, o MENOR manda no TIMING. Até aqui a análise nascia
// num frame só e comparar era trocar de chip cinco vezes guardando o resto na
// cabeça — e os planos DISCORDAM de verdade entre frames (mesmo ativo, mesmo dia:
// venda no semanal, invalidado no diário, compra no 1h).
//
// O QUE ISTO NÃO É: cinco cards empilhados. Cinco leituras com o mesmo peso na tela
// é ruído, e pior — cria cinco vereditos onde há um. Aqui é uma TABELA: uma linha
// por frame, colunas alinhadas, pra o olho DESCER a coluna e comparar (o mesmo
// motivo pelo qual a lista do scan virou grade). Três estados de linha, e eles são
// três coisas diferentes que a tela costumava embaralhar:
//
//   • VEREDITO   — a tupla que o motor elegeu. Uma só, marcada, em contraste cheio.
//   • ABERTO     — o frame que o gráfico está desenhando AGORA. Pode não ser o do
//                  veredito (o usuário clicou pra explorar), e por isso tem marca
//                  PRÓPRIA: confundir "o que estou vendo" com "o que decidiu" é
//                  exatamente o defeito que a spec de apresentação nomeia.
//   • EXPLORATÓRIO — os demais. Legítimos de ver, ilegítimos de operar como plano.
//
// Frame sem candle não some e não inventa: a linha fica, o estado diz "sem dado" e
// o motivo vai no title. Custa $0 de LLM e 1,3–1,9s a frio / 0,25–0,4s quente — os
// cinco em paralelo (ver ``runner.leitura_multiframe``, onde a medida está inteira).
let _escada = null;             // payload multiframe da run aberta (null = não há)

// Faixa declarada em ALL_TFS (macro × intra) virando os DOIS grupos de leitura do
// método: o macro é onde a TESE se decide, o intra é onde o TIMING se decide. Não é
// classificação nova — é a mesma que o seletor já usa pra separar as linhas.
const ESCADA_GRUPOS = [
  ["macro", "Tese", "os frames maiores mandam na direção do trade"],
  ["intra", "Timing", "os frames menores mandam na hora de entrar"],
];

// Uma linha da escada, NORMALIZADA para o método aberto. O gráfico desenha a
// leitura que dá nome ao método (DA-088) e a escada segue a mesma regra: numa run
// Storm123 as colunas são as do Storm; numa Setup123, as do 1-2-3 deste projeto.
// Somar os dois na mesma coluna misturaria setups diferentes no mesmo número — o
// que a task 008 já provou não descrever trade nenhum.
function escadaLeitura(f, metodo) {
  const tf = f.frame;
  if (f.estado === "sem_dado") {
    return { tf, estado: "sem_dado", motivo: f.motivo || "a fonte não tem candle deste ativo neste frame" };
  }
  if (metodo === "storm123") {
    const st = f.storm || {};
    return {
      tf, estado: st.estado || "sem_setup", direction: st.direction,
      trigger: st.trigger, sl: st.sl, tp: st.tp, rr: st.rr, rr_note: st.rr_note,
      dist_pct: st.dist_pct, opera: st.opera, veto: st.veto,
      entrada: st.entrada, eden: st.eden_rotulo, leituras: st.leituras,
    };
  }
  return {
    tf, estado: f.estado || "sem_setup", direction: f.direction,
    trigger: f.trigger, sl: f.sl, tp: f.tp, rr: f.rr, rr_note: f.rr_note,
    dist_pct: f.dist_pct, andado: f.andado_pct, invalidacao: f.invalidacao,
    pattern_state: f.pattern_state,
  };
}

// Uma leitura CONTA para a direção do grupo? Só quando existe padrão com direção
// OPERÁVEL. "sem setup" e "sem dado" não votam pelo motivo óbvio; o VETADO não vota
// pelo motivo que importa: o Éden proibiu, e o padrão continua tendo direção no
// detector. Deixá-lo votar faria o resumo dizer "2 de 2 de venda" sobre uma tese em
// que metade dos frames o método recusa operar — a mesma mentira de publicar o
// gatilho dele na linha. Por isso o resumo declara de quantos frames está falando.
function escadaVota(L) {
  return !!L.direction && L.estado !== "sem_dado" && L.estado !== "sem_setup"
    && L.estado !== "vetado";
}

// O RESUMO — a única coisa que a escada AFIRMA além de repetir os números. Ele só
// agrega direções já computadas (não é um sexto veredito): diz para onde apontam os
// frames de tese e os de timing, e se estão de acordo. Quando um grupo não tem
// leitura nenhuma, ele DIZ isso — grupo vazio é dado ausente, não empate.
function escadaResumoHtml(leituras) {
  const porFaixa = {};
  for (const [tf, , , faixa] of ALL_TFS) porFaixa[tf] = faixa || "intra";
  const blocos = [];
  for (const [faixa, nome, ajuda] of ESCADA_GRUPOS) {
    const doGrupo = leituras.filter((L) => porFaixa[L.tf] === faixa);
    if (!doGrupo.length) continue;
    const frames = doGrupo.map((L) => tfCurto(L.tf)).join(" · ");
    const votos = doGrupo.filter(escadaVota);
    const compra = votos.filter((L) => L.direction === "compra").length;
    const venda = votos.filter((L) => L.direction === "venda").length;
    let txt;
    let dir = null;
    if (!votos.length) {
      txt = `sem leitura em ${doGrupo.length === 1 ? "1 frame" : `nenhum dos ${doGrupo.length}`}`;
    } else if (compra && venda) {
      txt = `${compra} de compra × ${venda} de venda`;
    } else {
      dir = compra ? "compra" : "venda";
      txt = `${votos.length} de ${doGrupo.length} ${compra ? "de compra" : "de venda"}`;
    }
    blocos.push(
      `<span class="es-grupo" title="${escapeHtml(ajuda)}">` +
      `<span class="es-gk">${escapeHtml(nome)}</span>` +
      `<span class="es-gf">${escapeHtml(frames)}</span>` +
      `<b class="es-gv${dir ? " " + dir : ""}">${escapeHtml(txt)}</b></span>`
    );
  }
  // O ACORDO fala da ESCADA INTEIRA, não do par de grupos: um grupo rachado por
  // dentro (4h de venda × 1h de compra) é discordância tanto quanto tese contra
  // timing, e uma frase que só olhasse os dois grupos chamaria isso de nada.
  // Exige DUAS leituras pra existir — com uma só não há o que alinhar — e some
  // quando nenhum frame vota, porque aí a conclusão seria sobre dado ausente.
  const votos = leituras.filter(escadaVota);
  const lados = new Set(votos.map((L) => L.direction));
  let acordo = "";
  if (votos.length >= 2 && lados.size) {
    const ok = lados.size === 1;
    acordo = `<span class="es-acordo ${ok ? "ok" : "conflito"}" title="${escapeHtml(ok
      ? `os ${votos.length} frames com leitura apontam para o mesmo lado — é o caso em `
        + "que o método pede para operar a favor da tese e do timing juntos"
      : "os frames apontam para lados opostos: a leitura de um tempo gráfico contradiz "
        + "a de outro, e operar as duas é operar contra si mesmo")}">` +
      `${ok ? "alinhados" : "em conflito"}</span>`;
  }
  return `<div class="es-resumo">${blocos.join("")}${acordo}</div>`;
}

const ESCADA_COLUNAS = [
  ["frame", "Tempo gráfico da leitura"],
  ["papel", "Se esta leitura é a que decidiu (veredito) ou uma leitura exploratória"],
  ["estado", "Estado do setup naquele frame"],
  ["dist", "Distância do preço até o gatilho"],
  ["gatilho", "Nível que aciona a entrada"],
  ["SL", "Stop loss"],
  ["TP", "Alvo publicável — ou o motivo de não haver"],
  ["R:R", "Risco/retorno"],
];

function escadaCabecalhoHtml() {
  return `<div class="es-head-row">` + ESCADA_COLUNAS.map(([nome, ajuda]) =>
    `<span class="es-col" title="${escapeHtml(ajuda)}">${escapeHtml(nome)}</span>`).join("") + `</div>`;
}

// O PAPEL da linha, escrito. A cor sozinha não podia carregar isto: a paleta é
// semântica de PREÇO (verde/vermelho = alta/baixa, DA-078) e gastar verde em
// "este é o veredito" faria a tela dizer "alta" onde queria dizer "oficial".
function escadaPapelHtml(tf, veredito, aberto) {
  if (tf === veredito) {
    return `<span class="es-papel es-vered" title="${escapeHtml(
      "o veredito desta análise foi computado neste frame — é a leitura que decidiu")}">veredito</span>`;
  }
  if (tf === aberto) {
    return `<span class="es-papel es-aberto" title="${escapeHtml(
      "o gráfico acima está desenhando este frame agora — mas quem decidiu foi o frame do veredito")}">no gráfico</span>`;
  }
  return `<span class="es-papel es-explor" title="${escapeHtml(
    "leitura exploratória: legítima de ver, não é o plano da decisão")}">exploratório</span>`;
}

// Uma linha SEM níveis publicáveis (sem candle, sem padrão, invalidada, vetada pelo
// Éden) não deixa quatro células vazias na grade: o motivo ocupa o resto da fileira
// (`es-fim` termina na última coluna). Espremer "sem candle neste frame" na coluna
// de 52px da distância era truncar a única informação que aquela linha tem.
function escadaMotivoHtml(txt, title) {
  return `<span class="es-cell es-motivo es-fim"${title ? ` title="${escapeHtml(title)}"` : ""}>` +
    txt + `</span>`;
}

// O nome da coluna viaja DENTRO da célula (mesma técnica do `scan-ck` da lista):
// invisível enquanto a tabela é tabela — quem nomeia a coluna ali é o cabeçalho —,
// e visível quando a linha quebra no telefone. Número solto sem nome em cima não
// diz nada, e é exatamente isso que acontece quando o cabeçalho sai de cena.
function esCk(nome) {
  return `<span class="es-ck">${escapeHtml(nome)}</span>`;
}

function escadaCelulasHtml(L) {
  const vazia = `<span class="es-cell"></span>`;
  const dist = L.dist_pct != null
    ? `<span class="es-cell num">${esCk("dist")}${escapeHtml(fmtPctEscada(L.dist_pct))}</span>`
    : vazia;
  if (L.estado === "sem_dado") {
    // A célula da distância sai VAZIA (não há gatilho de que se medir distância),
    // mas EXISTE: é ela que ancora o motivo na coluna certa da grade.
    return vazia + escadaMotivoHtml(
      "sem candle neste frame — a fonte não cobre este tempo gráfico", L.motivo);
  }
  // VETADO pelo Éden: o nível existe no detector, mas a regra proíbe operar. Publicar
  // gatilho/SL/TP aqui seria oferecer um trade que o próprio método recusa.
  if (L.estado === "vetado") {
    // O nome do Éden vem PRONTO do produtor (`rotulo_curto`) e às vezes já traz a
    // palavra — "Éden de Alta" prefixado virava "Éden Éden de Alta". Prefixa só o
    // que precisa ("armadilha"), e nunca reescreve o rótulo (DA-095: um vocabulário).
    const nome = L.eden || "";
    const quem = !nome ? "Éden desalinhado"
      : /éden/i.test(nome) ? nome : `Éden ${nome}`;
    return dist + escadaMotivoHtml(`não opera — ${escapeHtml(quem)}`,
                                   L.veto || "o filtro Éden veta este lado");
  }
  if (L.estado === "sem_setup") {
    return dist + escadaMotivoHtml("sem padrão neste frame");
  }
  if (L.estado === "invalidou") {
    return dist + escadaMotivoHtml(
      L.invalidacao != null
        ? `invalidação <b>${scanFmt(L.invalidacao)}</b>` : "premissa rompida",
      "premissa rompida — o preço passou do nível de invalidação");
  }
  const tp = L.tp != null
    ? `<span class="es-cell num">${esCk("TP")}<b>${scanFmt(L.tp)}</b></span>`
    : `<span class="es-cell es-motivo" title="${escapeHtml("sem alvo — " + (L.rr_note || "nível de alvo indefinido"))}">sem alvo</span>`;
  return dist +
    `<span class="es-cell num">${esCk("gatilho")}<b>${scanFmt(L.trigger)}</b></span>` +
    `<span class="es-cell num">${esCk("SL")}<b>${scanFmt(L.sl)}</b></span>` + tp +
    `<span class="es-cell num${L.rr != null && L.rr < 1 ? " es-rr-baixo" : ""}">${esCk("R:R")}` +
    // R:R < 1 NUNCA em verde e sempre com a conta legível (invariante 7): aqui a
    // marca é a palavra no title, porque a célula tem largura de número.
    `${L.rr != null ? `<b title="${escapeHtml(L.rr < 1
      ? `abaixo de 1: o risco é maior que o retorno projetado neste frame`
      : "risco/retorno projetado do gatilho ao alvo")}">${scanFmt(L.rr)}</b>` : "—"}</span>`;
}

// Percentual da distância ao gatilho, na mesma forma que o scan usa (`_fmt_pct` no
// backend manda `dist_txt` pronto lá; aqui a escada recebe a fração crua e escreve
// com o mesmo desenho, pra as duas telas não terem dois jeitos de dizer "0,4%").
function fmtPctEscada(frac) {
  if (frac == null) return "—";
  return (frac * 100).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + "%";
}

function renderEscada(mf) {
  const el = $("escada");
  if (!el) return;
  _escada = mf && Array.isArray(mf.frames) && mf.frames.length ? mf : null;
  if (!_escada) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const metodo = mf.metodo || _openMethod || "setup123";
  const veredito = mf.veredito || _verdictTf;
  const leituras = mf.frames.map((f) => escadaLeitura(f, metodo));
  const linhas = leituras.map((L) => {
    const papel = L.tf === veredito ? "veredito" : L.tf === _tf ? "aberto" : "explor";
    const cls = ["es-row", `es-${papel}`, L.estado || "", L.direction || "",
                 L.tf === _tfPendente ? "es-pendente" : ""].filter(Boolean).join(" ");
    const chip = (L.estado === "sem_dado")
      ? `<span class="scan-chip">sem dado</span>`
      : metodo === "storm123"
      ? `<span class="scan-chip es-storm ${escapeHtml(L.estado)}">${escapeHtml(SCAN_STORM_ESTADO[L.estado] || L.estado)}</span>`
      : scanEstadoChip(L.estado, L.direction, L.andado);
    return `<button type="button" class="${cls}" data-es-tf="${escapeHtml(L.tf)}" ` +
      `title="${escapeHtml(`Recalcular o gráfico e os cards no ${tfNome(L.tf)}`)}">` +
      `<span class="es-cell es-tf"><b>${escapeHtml(tfCurto(L.tf))}</b>` +
      // "S" e "D" precisam do nome por extenso quando a linha quebra e não há
      // cabeçalho; "4h"/"1h"/"15m" já SÃO o nome — repeti-los saía "4h 4h".
      (tfNome(L.tf) !== tfCurto(L.tf)
        ? `<span class="es-tf-nome">${escapeHtml(tfNome(L.tf))}</span>` : "") +
      `</span>` +
      `<span class="es-cell es-papel-cell">${escadaPapelHtml(L.tf, veredito, _tf)}</span>` +
      `<span class="es-cell es-estado">${chip}</span>` +
      escadaCelulasHtml(L) + `</button>`;
  }).join("");
  const custo = mf.ms != null
    ? `<span class="es-custo" title="${escapeHtml(
        "os cinco frames vão em paralelo e leem a mesma série cacheada da análise — $0 de LLM")}">` +
      `os ${mf.frames.length} frames em ${(mf.ms / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}s · $0</span>`
    : "";
  el.innerHTML =
    `<div class="es-topo"><h2 class="section-title">A escada — ${escapeHtml(methodLabel(metodo))} ` +
      `nos ${mf.frames.length} tempos gráficos</h2>${custo}</div>` +
    escadaResumoHtml(leituras) +
    `<p class="es-nota">O veredito é <b>um</b> frame — o ${escapeHtml(tfNome(veredito))}. ` +
    `Os outros são leituras exploratórias do mesmo método: legítimas de ver, ` +
    `não são o plano da decisão. Clique num degrau para o gráfico e os cards abaixo ` +
    `passarem para ele.</p>` +
    escadaCabecalhoHtml() + `<div class="es-rows">${linhas}</div>`;
  el.querySelectorAll("[data-es-tf]").forEach((b) =>
    b.addEventListener("click", () => switchTimeframe(b.dataset.esTf)));
  el.classList.remove("hidden");
}

// ───────────────────────── CARD DE EXECUÇÃO (task 012) ──────────────────────
//
// "Quero um card explicando as entradas alvos como inserir as ordens e onde colocar
// SL, TPS e onde invalida, e se ainda vale a pena entrar, ou se é pra aguardar recuo
// até faixa tal." O print que abriu a task mostra por quê: nove faixas de três
// famílias na tela e nenhuma frase dizendo o que FAZER com elas.
//
// Toda a POLÍTICA é decidida no backend (webui/execucao.py, modelada da spec do
// degenbot sobre o corpus do Erick) — aqui é só desenho. Um veredito calculado em
// dois lugares vira dois vereditos.
const VEREDITO_CLS = { entrar: "ok", aguardar: "espera", passar: "nao", sem_setup: "nao" };

function renderExecCard(dados) {
  const el = $("execCard");
  if (!el) return;
  const c = dados && dados.card;
  if (!c || !c.veredito || c.veredito.estado === "sem_setup") {
    el.classList.add("hidden"); el.innerHTML = ""; return;
  }
  const v = c.veredito;
  const linha = (k, valor, base, cls) =>
    `<div class="ex-row${cls ? " " + cls : ""}"><span class="ex-k">${escapeHtml(k)}</span>` +
    `<b class="ex-v">${escapeHtml(valor)}</b>` +
    (base ? `<span class="ex-base">${escapeHtml(base)}</span>` : "") + "</div>";

  // 1) O VEREDITO é a manchete: é a pergunta que ele fez olhando o gráfico.
  const cab =
    `<div class="ex-head"><span class="ex-title">Como operar</span>` +
    `<span class="ex-vered ${VEREDITO_CLS[v.estado] || ""}">${escapeHtml(v.rotulo)}</span></div>` +
    `<div class="ex-motivo">${escapeHtml(v.motivo)}</div>`;

  // 2) AS ORDENS, na sequência em que se digitam. O passo numerado é o ponto: não é
  // uma lista de níveis, é um roteiro.
  const ordens = (c.ordens || []).map((o) =>
    `<div class="ex-ordem"><span class="ex-passo">${o.passo}</span>` +
    `<span class="ex-tipo">${escapeHtml(o.tipo)}</span>` +
    `<span class="ex-papel">${escapeHtml(o.papel)}</span>` +
    `<b class="ex-preco">${fmtNum(o.price)}</b>` +
    (o.fracao ? `<span class="ex-fracao">${escapeHtml(o.fracao)}</span>` : "") +
    `<span class="ex-base">${escapeHtml(o.base || "")}</span></div>`).join("");

  const inv = c.invalidacao || {};
  const linhas = [];
  if (inv.price != null) {
    linhas.push(linha("invalida em", fmtNum(inv.price),
      inv.meaning || "onde o setup deixa de existir", "ex-inval"));
  }
  if (c.saida) linhas.push(linha("realizar", c.saida.texto, c.saida.calibrar || ""));
  if (c.peso) linhas.push(linha("peso", c.peso.degrau, `${c.peso.motivo} · ${c.peso.nota}`));

  // 3) PROTEÇÃO — desligada por default, e o card diz POR QUÊ. Um default silencioso
  // aqui seria o pior dos mundos: o método compra o recuo à média, e um BE/trailing
  // ligado ejeta no pullback em que se adiciona.
  const p = c.protecao || {};
  const prot = ["be", "trailing"].filter((k) => p[k]).map((k) => {
    const x = p[k];
    const como = k === "be"
      ? (x.gatilhos || []).map((g) => g.texto).join(" · ")
      // sem a nota aqui: ela sai na linha própria logo abaixo, e repetida vira a
      // mesma frase duas vezes no mesmo bloco
      : `${x.referencia} — ${x.disparo}`;
    return `<div class="ex-prot"><span class="ex-k">${escapeHtml(x.rotulo)}</span>` +
      `<span class="ex-estado ${x.ligado ? "on" : "off"}">${x.ligado ? "ligado" : "desligado"}</span>` +
      `<span class="ex-base">ligar ${escapeHtml(como)}</span>` +
      // A NOTA é o porquê do default, e é a parte que importa: sem ela, "desligado"
      // parece descuido em vez de decisão de método.
      `<span class="ex-base">${escapeHtml(x.nota || "")}</span>` +
      `<span class="ex-evid">${escapeHtml(x.evidencia || "")}</span></div>`;
  }).join("");

  el.innerHTML = cab +
    (ordens ? `<div class="ex-ordens"><span class="ex-sec">ordens, na sequência</span>${ordens}</div>` : "") +
    (linhas.length ? `<div class="ex-rows">${linhas.join("")}</div>` : "") +
    (prot ? `<div class="ex-prots"><span class="ex-sec">proteção</span>${prot}</div>` : "") +
    confiabilidadeHtml(c.confiabilidade);
  el.classList.remove("hidden");
}

// O ÍNDICE — e o gate de N é o ponto dele. Taxa de acerto com 3 casos é ruído que
// engana mais do que ajuda, então abaixo do mínimo a tela DIZ que não há amostra em
// vez de exibir um número. E lidera pela EXPECTATIVA: 70% de acerto com R:R 0,13
// perde dinheiro.
function confiabilidadeHtml(conf) {
  if (!conf || !conf.setups) return "";
  const nomes = { "123": "Setup123", storm: "Storm123" };
  // O nível é uma CHAVE do backend ("operavel"), e ela estava indo crua pra tela —
  // a pílula dizia "OPERAVEL", sem acento, no meio de um card escrito em português.
  const niveis = { insuficiente: "sem amostra", preliminar: "preliminar",
                   operavel: "operável" };
  const blocos = Object.entries(conf.setups).map(([k, s]) => {
    const cab = `<span class="ex-k">${escapeHtml(nomes[k] || k)}</span>` +
      `<span class="ex-nivel ${escapeHtml(s.nivel)}">${escapeHtml(niveis[s.nivel] || s.nivel)}</span>` +
      `<span class="ex-base">${escapeHtml(s.texto || "")}</span>`;
    if (s.nivel === "insuficiente") {
      return `<div class="ex-conf">${cab}` +
        `<span class="ex-base">${s.n_fechados} fechado(s) de ${s.n} gatilho(s) logado(s)</span></div>`;
    }
    // O SINAL DA EXPECTATIVA TEM DE SE LER. Ela lidera o bloco justamente porque é ela
    // que responde "isso ganha dinheiro?" — e saía no mesmo cinza-claro fosse +0,35 ou
    // −0,42, com a pílula VERDE de "operável" logo acima (que qualifica a AMOSTRA, não
    // o setup). Verde em cima de expectativa negativa é a cor afirmando o contrário do
    // número, o defeito que a DA-078 nomeou. Negativa não ganha cor nova — ganha a
    // PALAVRA, que é a regra 3 da mesma decisão.
    const negativa = s.expectativa_r != null && s.expectativa_r < 0;
    const er = s.expectativa_r != null
      ? `<b class="ex-v${negativa ? " ex-neg" : ""}">E[R] ${fmtNum(s.expectativa_r)}</b>` +
        (negativa
          ? `<span class="ex-alerta">expectativa NEGATIVA — do jeito que foi medido, ` +
            `este setup perde dinheiro por trade</span>` : "") +
        `<span class="ex-base">por trade, em múltiplos de risco` +
        (s.rr_medio != null ? ` · R:R médio ${fmtNum(s.rr_medio)}` : "") +
        (s.acerto_equilibrio != null
          ? ` · precisa acertar ${pctBR(s.acerto_equilibrio * 100)}% só pra empatar` : "") +
        `</span>`
      : `<span class="ex-base">expectativa sem base (nenhum fechado com R:R conhecido)</span>`;
    const ic = s.ic95
      ? ` <span class="ex-ic">(${(s.ic95[0] * 100).toFixed(0)}–${(s.ic95[1] * 100).toFixed(0)}%, intervalo 95%)</span>`
      : "";
    const taxa = s.taxa_acerto != null
      ? `<span class="ex-base">acerto ${(s.taxa_acerto * 100).toFixed(0)}% em ${s.n_fechados} fechados${ic}</span>`
      : "";
    return `<div class="ex-conf">${cab}${er}${taxa}</div>`;
  }).join("");
  return `<div class="ex-confs"><span class="ex-sec">confiabilidade — por setup, ` +
    `${conf.n_minimo}+ fechados pra exibir taxa</span>${blocos}</div>`;
}

// Busca o card do ativo aberto. Falha é SILENCIOSA na tela (o card some), nunca um
// erro vermelho sobre uma análise que está inteira: o card é leitura adicional.
async function carregaExecCard() {
  const el = $("execCard");
  if (!_openTicker) { if (el) { el.classList.add("hidden"); el.innerHTML = ""; } return; }
  const q = new URLSearchParams({ ticker: _openTicker, date: _openDate || "",
                                  tf: _tf, method: _openMethod || "padrao" });
  try {
    const res = await fetch("/api/execucao?" + q.toString());
    const data = await res.json();
    if (!res.ok || data.error) { if (el) { el.classList.add("hidden"); el.innerHTML = ""; } return; }
    renderExecCard(data);
  } catch (err) {
    if (el) { el.classList.add("hidden"); el.innerHTML = ""; }
  }
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
// A EMA 80 é a LENTA do Éden (setup Storm) — só é desenhada nas runs Storm
// (ver _chart_emas no backend), e ganha cor própria pra não se confundir com as
// de timing (8/21/50).
//
// A MÉDIA NÃO É GANHO (task 20260831-005). A EMA8 era a única média pintada com uma
// cor que SIGNIFICA: verde é ganho/alta na tela inteira (DA-078 regra 3), e uma média
// é estrutura — o "todo o resto é branco e cinza em níveis" da mesma regra. O custo
// disso não era só conceitual, e está medido: a distância CIELAB de cada média até a
// COR DA VELA que ela atravessa —
//
//   EMA8 #4be3a0 × vela de alta ......... ΔE  16,0   ← some dentro do corpo verde
//   EMA21 #e3894b × vela de baixa ....... ΔE  40,4
//   MMS20 #f5b445 × vela de baixa ....... ΔE  64,3
//   EMA80 #7cb0ff × vela de baixa ....... ΔE  91,4
//   MMS50 #6ea8fe × vela de alta ........ ΔE 105,5
//
// — a EMA8 estava 2,5x mais perto da cor da vela que a segunda pior, e num gráfico de
// alta ela cruza corpo verde o tempo todo. Em `#e6eaf2` (o `--text` da paleta, já usado
// no canvas na pílula do preço atual) ela fica a ΔE 73,9 da vela de alta e 75,9 da de
// baixa: é a única média com boa distância das DUAS cores de vela. Contra as irmãs o
// mínimo é 45,6 (EMA80) — nenhuma colisão nova. Nenhuma cor nova entra.
const EMA_COLORS = { "8": "#e6eaf2", "21": "#e3894b", "50": "#e34bd0", "80": "#7cb0ff" };
// 1-2-3 marker colour by direction — distinct so compra (fundo) and venda (topo)
// never read the same on the chart. Blue for compra, orange for venda; both stay
// clear of the green/red candle bodies.
const PAT_COLORS = { compra: "#6ea8fe", venda: "#ff9f43" };
// PADRÃO MORTO É FANTASMA. Um 1-2-3 que perdeu o ponto 3 continuava desenhado com a
// MESMA cor e o mesmo peso de um vivo — e a cor é a primeira coisa que se lê. O morto
// sai do vocabulário de cor dos vivos (azul de compra / laranja de venda) e vira
// cinza apagado: continua na tela, porque a história explica onde o preço está, mas
// para de competir com o que ainda vale.
const COR_FANTASMA = "#6b7280";
// ESMAECIDO NÃO É APAGADO. O fantasma era pintado a 0,45 de opacidade — e o painel do
// gráfico é PRETO PURO, onde opacidade é multiplicação em direção ao fundo: o cinza
// saía da tela valendo (78,82,93), 2,7:1 de contraste, abaixo do piso de 3:1 que a
// WCAG 1.4.11 pede pra um objeto gráfico ser percebido. O preço ao lado do ponto,
// mais fino, ficava em 2,1:1. No celular, no meio das velas, isso é sumir — e a nota
// abaixo do gráfico prometia textualmente que "os pontos ficam em cinza como
// história" (task 048).
//
// A subordinação do morto NÃO precisava daquela transparência: ela já é carregada
// pela COR (este cinza rende 4,4:1 contra os 9,5:1 do azul do Storm — menos da metade
// do peso), pela FORMA do marcador e pela palavra "invalidado". A opacidade que resta
// é a mínima que ainda diz "menos" sem apagar, e o teste do módulo
// ``test_webui_fantasma_legivel_e2e`` mede o pixel: 3:1 de piso, e o morto obrigado a
// ficar abaixo de 70% do contraste do vivo.
const ALFA_FANTASMA = 0.85;

function ehFantasma(pat) {
  return !!(pat && pat.invalidado);
}

function patColor(pat) {
  if (ehFantasma(pat)) return COR_FANTASMA;
  return (pat && PAT_COLORS[pat.direction]) || "#6ea8fe";
}

// O FANTASMA VALE PARA AS DUAS LEITURAS. O cinza do morto nasceu no 1-2-3 de
// swings e ficou só lá: no gráfico, um Storm invalidado continuava com o azul de
// um Storm vivo — e o Storm é o método mais usado. A regra é a mesma dos dois
// lados, então a cor sai da mesma função.
//
// O PONTO SEGUE DIREÇÃO, IGUAL AO SETUP123 (task 20260831-004 — Samyr: "é só usar o
// mesmo padrão de cor do Setup123"). Até aqui o Storm pintava o marcador por
// FAMÍLIA (um azul só, compra ou venda) enquanto o Setup123 pinta por DIREÇÃO
// (PAT_COLORS). Duas gramáticas de cor na mesma tela — e uma delas colidia com o
// verde de outros marcadores (task 003). Uma gramática só: a cor do PONTO diz a
// direção em qualquer família; quem separa família é o traço (ponto-traço) e a
// forma (losango × círculo), que não competem com a cor.
function stormColor(pat) {
  return patColor(pat);
}

// OS TRÊS ESTADOS DO STORM NO GRÁFICO — e nenhum deles é "sumir".
//
// O padrão vetado pelo Éden era DETECTADO, DESCRITO no card com todos os níveis e
// simplesmente não desenhado. O usuário lia "Storm123 de compra · NÃO OPERA" no card
// e não achava nada na vela: a tela contradizendo a si mesma. E ver o setup que NÃO se
// opera é parte de aprender a reconhecê-lo — é literalmente o que o card já faz em
// texto.
//
//   operável   — cor do Storm, contorno sólido, NÍVEIS na tela
//   vetado     — mesma cor (a estrutura é real e atual), peso menor, contorno
//                TRACEJADO e a palavra "não opera — Éden". SEM níveis: o gráfico é a
//                figura operável, e traçar gatilho/alvo/stop de um trade que a regra
//                proíbe é convidar a operá-lo
//   invalidado — CINZA (fora do vocabulário de cor dos vivos), e a palavra
//                "invalidado" (DA-091). O cinza já pesa metade do azul do método na
//                tela; o que ele não pode é cair abaixo do piso de legibilidade —
//                ver ALFA_FANTASMA
//
// Precedência: morto ganha de vetado. Uma vez morto, morto — o veto descreve um setup
// que ainda existe; a invalidação, um que não existe mais.
function stormEstado(storm) {
  if (!storm || !storm.pattern) return null;
  if (ehFantasma(storm.pattern)) return "invalidado";
  return storm.opera === true ? "operavel" : "vetado";
}

// A palavra do estado, pra etiqueta na vela e pra legenda. Vazia no operável: o
// normal não se anuncia, só o que desvia dele.
//
// No VETADO ela diz QUAL Éden vetou — "não opera — armadilha" e "não opera — Éden de
// Baixa" são vetos diferentes, e o segundo é o único que se resolve esperando. O nome
// vem do vocabulário único (forma CURTA: aqui o espaço é a largura de uma vela) e,
// alinhado, ganha o CONTRASTE de :func:`edenContraste` — ver ali o porquê.
function stormEstadoTexto(estado, storm) {
  if (estado === "invalidado") return "invalidado";
  if (estado !== "vetado") return "";
  const st = storm || {};
  const eden = st.eden || {};
  const nome = edenCurto(eden);
  if (!nome) return "não opera — Éden";
  const dir = st.pattern && st.pattern.direction;
  return `não opera — ${edenContraste(eden, dir, nome)}`;
}

// FORMA DO MARCADOR = FAMÍLIA. Os dois métodos numeram 1-2-3 pontos DIFERENTES (no
// Setup123 o ponto 2 é o topo do repique e o 3 um fundo ascendente; no Storm o 2 é
// o EXTREMO do movimento e o 3 a tentativa que falha), e as cores não separam: o
// azul de compra do Setup123 (#6ea8fe) e o azul do Storm (#7cb0ff) são o mesmo azul
// a um palmo de distância. Com as duas camadas ligadas, ①②③ de um viraria ①②③ do
// outro. A FORMA separa antes da cor — círculo é Setup123, losango é Storm123 — e a
// legenda carrega a mesma forma, então o vínculo se lê sem decorar nada.
const FORMA_DA_FAMILIA = { plano: "circulo", storm: "losango" };
// TRAÇO = FAMÍLIA nos NÍVEIS, pelo mesmo motivo que a forma separa os marcadores.
// Os níveis do Storm eram TODOS azuis (a cor dizia de quem era a linha) e o traço
// dizia o papel — com o stop do Storm em [6,4], EXATAMENTE o traço do stop do plano.
// Ou seja: o traço já não separava família nenhuma, e a cor pintava por
// PERTENCIMENTO em cima da regra de que cor é significado (DA-078 regra 3). Os dois
// papéis foram trocados: a COR volta a dizer o que o nível SIGNIFICA (vermelho =
// onde se perde, verde = onde se ganha) e o TRAÇO passa a dizer de QUEM é.
// Ponto-traço porque nenhum nível do plano usa ritmo composto — é reconhecível de
// relance e não colide com [6,4], [5,4], [2,3] nem [5,3].
const TRACO_STORM = [7, 3, 2, 3];
// Faixas do plano acionável desenhadas no gráfico: compra (verde), realização /
// alvo (dourado), recuo a aguardar (púrpura, só quando difere da compra) e os
// níveis que tornam o setup operável — invalidação (vermelho claro, pontilhado:
// onde o 1-2-3 deixa de existir) e stop (vermelho, tracejado: a invalidação com a
// folga de ATR). O ALVO reusa o dourado da realização de propósito: é a mesma
// função (onde se realiza), e quando os dois são o mesmo nível vira UMA faixa só.
// UM SIGNIFICADO, UM VERDE (task 20260831-005). O alvo tinha verde PRÓPRIO
// (``#26de81``) ao lado do verde de ganho/alta da tela inteira (``#2ecc71``, o
// ``--green`` do CSS, que pinta o candle de alta, a faixa de compra, a bolinha de
// recuo, o "ativo" do scan). Medido em CIELAB: **ΔE 7,2** entre os dois — 2,2° de
// matiz de diferença, indistinguíveis a olho em qualquer tamanho, e mais ainda no
// celular. Dois hexes para o MESMO significado (o alvo É ganho) não separam nada:
// só fazem a tela ter dois verdes onde tem um conceito. O alvo passa a usar o verde
// da paleta, e o canvas finalmente concorda com o CSS.
const ZONE_COLORS = { buy: "#2ecc71", realize: "#f5b445", pullback: "#c084fc",
                      stop: "#ff5c6c", invalid: "#ff9aa6", resist: "#8b97ad",
                      target: "#2ecc71", storm: "#7cb0ff", inativa: "#8b97ad",
                      projecao: "#9aa4b8" };
// ``inativa`` é o cinza de quem NÃO é entrada agora. A faixa da média saía verde
// com o rótulo "não ativa agora" escrito nela — a cor afirmando o contrário do
// texto, e verde é "pode ir" na tela inteira. Tracejado e apagado não bastavam:
// eram diferença de ACABAMENTO dentro da mesma cor, e a cor é o que se lê primeiro.
// Cor POR PAPEL: vermelho para o que tira do trade (invalidação clara, stop
// forte), verde-alvo para o TP, dourado para a realização quando não há padrão,
// cinza-neutro para o topo overhead que NÃO é alvo (setup de venda) — ali ele é
// contexto de estrutura, não nível de ação. Cada faixa/linha ainda carrega o
// PRÓPRIO rótulo desenhado no gráfico, então cor parecida nunca vira dúvida.
// O estado de cada gráfico (dados + janela de zoom h/v + geometria) mora no próprio
// elemento <canvas> (canvas._chart/_actionable/_view/_vview/_yGeom/_autoY), pra que o
// gráfico principal e os dois da comparação sejam independentes.
let _tf = "1d";               // timeframe atualmente exibido no gráfico principal
let _timeframes = ["1d"];     // frames operáveis do ativo aberto (ação e cripto = escada inteira)
let _openDate = "";           // data da análise aberta (recomputa por timeframe)
let _assetType = "";          // tipo do ativo aberto (define a fonte do intradiário)
let _verdictTf = "1d";        // timeframe em que o veredito ABERTO foi computado (carimbo)
// Frame CLICADO cuja resposta ainda não chegou. O ATIVO continua sendo _tf (o que
// está desenhado) — é essa separação que impede a tela de afirmar um frame e
// mostrar os níveis de outro. `_tfSeq` sela cada pedido: resposta de troca superada
// não pinta nada.
let _tfPendente = null;
let _tfSeq = 0;
// O frame que está DE FATO desenhado no canvas. Diferente de `_tf` (o frame cujos
// NÍVEIS estão na tela): quando a carga volta sem velas, os níveis trocam e o
// desenho não — e sem os dois separados a tela afirmava um frame mostrando o
// desenho de outro, e o reclique no mesmo frame virava no-op (DA-118).
let _tfDesenhado = null;
// Revalidação automática em voo. Não se empilha: uma resposta lenta não pode
// gerar uma fila de recargas do mesmo frame.
let _revalEmVoo = false;
let _revalTimer = null;
// Cotação da run ABERTA. Não é propriedade do frame — o /api/chart não a devolve, e
// sem lembrá-la a unidade sumia da tira ao trocar de timeframe.
let _openLive = null;

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
// FONTE ÚNICA dos timeframes: código · rótulo CURTO (a pill) · nome COMPLETO
// (prosa, title, aria) · FAIXA (a linha do bloco TEMPO na barra). Os DOIS
// seletores — o da barra de controle e o do gráfico — leem daqui, e
// TF_LABEL/TF_SHORT são DERIVADOS da mesma lista: nada de listas paralelas
// mantidas à mão, que foi como o rótulo curto da task 012 chegou só no scan e
// nunca na barra. O curto é o que ocupa espaço; o completo é o que explica.
// A FAIXA separa o macro do intradiário e é declarada AQUI, no próprio frame —
// não numa lista de "quem vai em cima": frame novo entra na linha certa sozinho.
const ALL_TFS = [
  ["1w", "S", "Semanal", "macro"],
  ["1d", "D", "Diário", "macro"],
  ["4h", "4h", "4h", "intra"],
  ["1h", "1h", "1h", "intra"],
  ["15m", "15m", "15m", "intra"],
];
const TF_LABEL = Object.fromEntries(ALL_TFS.map(([tf, , completo]) => [tf, completo]));
const TF_SHORT = Object.fromEntries(ALL_TFS.map(([tf, curto]) => [tf, curto]));

// ─────────────── O VOCABULÁRIO ÚNICO DE TIMEFRAME (invariante 6) ─────────────
//
// A tela tinha TRÊS formas concorrentes de escrever o mesmo frame: o carimbo do
// cabeçalho e o selo do gráfico liam `TF_LABEL` ("Diário"), enquanto o bloco de
// cards ecoava a prosa do backend ("diário (referência) · semanal (tendência de
// fundo)"). Três nomes pro mesmo frame na mesma tela é como o bug do frame nasceu:
// cada correção inventava o seu jeito de dizer timeframe, e ninguém conseguia
// comparar duas superfícies.
//
// A partir daqui há UM lugar canônico. `tfNome` é o nome de exibição e `tfCurto` a
// forma de botão; TODA superfície consome deles, nunca de uma string do payload.
// A prosa do backend não se perde — ela é CONTEÚDO (o diário lê o semanal como
// tendência de fundo), não o nome do frame, e vai pro `title` de quem a exibia.
function tfNome(tf) {
  return TF_LABEL[tf] || tf || "";
}

function tfCurto(tf) {
  return TF_SHORT[tf] || tf || "";
}

// As linhas do bloco TEMPO saem da faixa declarada em ALL_TFS, na ordem em que
// cada faixa aparece — quem acrescentar um frame não precisa lembrar de mexer aqui.
function tfFaixas() {
  const linhas = [];
  for (const item of ALL_TFS) {
    const faixa = item[3] || "intra";
    let linha = linhas.find((l) => l.faixa === faixa);
    if (!linha) linhas.push((linha = { faixa, itens: [] }));
    linha.itens.push(item);
  }
  return linhas;
}

// Legenda do gráfico (swatches das MMS/EMA + faixas do plano + 1-2-3). Extraída
// pra ser reusada pelos mini-gráficos da comparação.
function chartLegendHtml(chart, actionable) {
  const zones = planZones(actionable);
  // A legenda descreve o DESENHO. Listar sete médias com três traçadas é pior que
  // não ter legenda: ela vira uma lista do que o backend sabe, não do que está lá.
  const med = mediasVisiveis(actionable);
  const wins = (chart.ma_windows || [20, 50, 200]).map(String).filter((w) => med.ma.has(w));
  const ewins = (chart.ema_windows || []).map(String).filter((w) => med.ema.has(w));
  const pat = camadaVisivel("plano") ? (chart.markers && chart.markers.pattern_123) : null;
  const legend = [];
  wins.forEach((w) => {
    if (MA_COLORS[w]) legend.push(`<span class="lg"><span class="sw" style="background:${MA_COLORS[w]}"></span>MMS${w}</span>`);
  });
  ewins.forEach((w) => {
    if (EMA_COLORS[w]) legend.push(`<span class="lg"><span class="sw" style="background:${EMA_COLORS[w]}"></span>EMA${w}</span>`);
  });
  // A AMOSTRA DA LEGENDA CARREGA O TRAÇO, não só a cor — mesma razão de ela carregar
  // a FORMA do marcador logo abaixo. Com stop e alvo do Storm agora em vermelho e
  // verde (cor = significado), duas linhas da legenda passam a ter a mesma cor de
  // duas do plano; é o ponto-traço que diz de quem é cada uma, e ele precisa estar
  // AQUI, onde se decodifica o gráfico, e não só no gráfico.
  zones.forEach((z) => legend.push(
    `<span class="lg"><span class="sw band${z.familia === "storm" ? " storm" : ""}" ` +
    `style="background:${z.color}"></span>${escapeHtml(z.tag)}</span>`));
  // NÍVEL RECUSADO ENTRA NA LEGENDA (DA-123). A legenda descreve o desenho, e a
  // AUSÊNCIA de um nível que o leitor espera encontrar é parte do desenho: sem
  // esta linha, "cadê o alvo?" não tem resposta em lugar nenhum do gráfico. A
  // amostra vai VAZIA (contorno tracejado) porque não há linha traçada a
  // decodificar — é isso que ela diz. Sem cor nova (DA-078 regra 3): o que
  // distingue é a forma da amostra e a palavra.
  if (camadaVisivel("plano")) {
    niveisRecusados(actionable).forEach((n) => legend.push(
      `<span class="lg lg-sem" title="${escapeHtml(n.motivo)}">` +
      `<span class="sw vazia"></span>${escapeHtml(n.nome)} — sem alvo: ` +
      `${escapeHtml(motivoCurto(n.motivo))}</span>`));
  }
  // As bolinhas verdes na mínima da vela (``chart.markers.buy_regions``) marcam
  // TOQUES PASSADOS na média — nada delas está na legenda hoje, e círculo vivo sem
  // rótulo ao lado do losango apagado do Storm lê como "onde estão os números do
  // padrão?" (não são: não têm número, são histórico). A cor já é única (nenhum
  // outro marcador usa este verde) — falta só a chave que liga cor a nome.
  const nRegioes = (chart.markers && chart.markers.buy_regions || []).length;
  if (nRegioes) {
    legend.push(`<span class="lg"><span class="sw dot" style="background:${ZONE_COLORS.buy}">` +
      `</span>recuo à média (histórico)</span>`);
  }
  // A legenda carrega a FORMA do marcador, não só a cor: é ela que separa as duas
  // numerações no candle, e uma legenda que só mostra cor deixaria o leitor sem a
  // chave da desambiguação. "invalidado" entra no texto porque o cinza sozinho
  // pede que se saiba de cor o que ele significa.
  if (pat) {
    const [, dlabel] = PAT_DIR[pat.direction] || ["", ""];
    const q = familiasNaTela(actionable).length > 1 ? "Setup123 " : "";
    const morto = ehFantasma(pat) ? " (invalidado)" : "";
    legend.push(`<span class="lg"><span class="sw dot" style="background:${patColor(pat)}"></span>${q}1-2-3 ${escapeHtml(dlabel)}${morto}</span>`);
  }
  const est = camadaVisivel("storm") && actionable ? stormEstado(actionable.storm) : null;
  if (est) {
    const sp = actionable.storm.pattern;
    const [, dlabel] = PAT_DIR[sp.direction] || ["", ""];
    const q = familiasNaTela(actionable).length > 1 ? "Storm123 " : "";
    const txt = stormEstadoTexto(est, actionable.storm);
    legend.push(`<span class="lg"><span class="sw dia" style="background:${stormColor(sp)}"></span>${q}1-2-3 ${escapeHtml(dlabel)}${txt ? ` (${escapeHtml(txt)})` : ""}</span>`);
  }
  return legend.join("");
}

// Desenha o gráfico. Devolve **true se desenhou** — quem chama precisa saber, e
// hoje ninguém sabia (DA-118).
//
// DUAS coisas que esta função fazia e não devia:
//
// 1. **Sumia com o gráfico** quando a resposta vinha sem velas: `card.hidden` +
//    `cv._chart = null`. O usuário trocava de frame, a fonte intradiária daquele
//    frame não respondia, e a tela ficava sem gráfico nenhum — a leitura dele foi
//    "às vezes mudo o timeframe e o gráfico não muda". O que já se sabe não se
//    apaga por causa de uma atualização que não chegou: o gráfico ANTERIOR fica, e
//    quem chama DECLARA que ele é de outro frame. Some só se nunca houve gráfico.
// 2. **Zerava o zoom** (`_view`/`_vview`) em toda pintura, inclusive quando o
//    assunto era o MESMO (ativo + frame) — ou seja, a cada revalidação. Ajustar o
//    enquadramento e perdê-lo sozinho a cada minuto é o "piscar" reclamado. Agora
//    a vista só reinicia quando o assunto muda; revalidação atualiza EM LUGAR.
function renderChartCard(chart, ticker, actionable, tf) {
  const card = $("chartCard");
  const cv = $("priceChart");
  const hasData = chart && Array.isArray(chart.candles) && chart.candles.length > 2;
  if (!hasData) {
    // Nunca houve gráfico: não há o que preservar, e o card vazio é a verdade.
    if (!cv || !cv._chart) { card.classList.add("hidden"); if (cv) cv._chart = null; }
    return false;
  }
  const mesmoAssunto = cv._chart && cv._ticker === ticker && cv._tf === (tf || cv._tf);
  if (!mesmoAssunto) { cv._view = null; cv._vview = null; }
  cv._chart = chart;
  cv._actionable = actionable || null;
  cv._ticker = ticker;
  if (tf) cv._tf = tf;
  card.classList.remove("hidden");

  const active = chart.markers && chart.markers.active_region;
  const pat = chart.markers && chart.markers.pattern_123;
  card.classList.toggle("setup-active", !!active || (pat && pat.state === "acionado"));

  // As faixas do plano são as mesmas do plano acionável, agora desenhadas na
  // linha do preço em vez de repetidas em texto. buy/pullback coincidem no caso
  // "aguardar recuo" (mesma média) — desenha-se uma só (ver drawPriceChart).
  const zones = planZones(actionable);
  renderCamadasSelector(actionable);
  renderCamadasAviso(actionable);
  $("chartLegend").innerHTML = chartLegendHtml(chart, actionable);

  // NOTA DO GRÁFICO — o que está DESENHADO, e só isso (task 021).
  //
  // Ela listava, em prosa, os mesmos preços que o canvas já pinta na linha e que o
  // card da análise agora carrega com a base ao lado: gatilho, invalidação, stop,
  // alvo e R:R saíam aqui pela TERCEIRA vez. Três cópias do mesmo número não são
  // redundância inofensiva — é o leitor tendo que conferir se as três dizem a mesma
  // coisa. As bases ("invalidação + folga de 0.5·ATR14", "topo anterior …") não se
  // perderam: desceram pro card, coladas no número que justificam.
  //
  // Fica aqui só o que é sobre o DESENHO e não sai em nenhum card: quantas regiões
  // de recuo o período marcou e que as faixas estão rotuladas na própria linha do
  // preço — mais o estado vazio, quando não há nada marcado.
  const notes = [];
  const nreg = (chart.markers && chart.markers.buy_regions || []).length;
  if (nreg) notes.push(`${nreg} região(ões) de <b>recuo à média</b> marcada(s) no período.`);
  const mortos = fantasmasNaTela(chart, actionable);
  if (mortos.length) {
    notes.push(`${mortos.join(" e ")} <b>invalidado</b> — os pontos ficam em cinza como ` +
      `história, e os níveis dele saem do gráfico: descrevem um trade que não existe mais.`);
  }
  if (stormVetadoNaTela(actionable)) {
    const st = actionable.storm || {};
    const veto = st.veto || st.motivo || "";
    // O NOME uma vez, entre parênteses, e a prosa do veto logo em seguida: a prosa já
    // costuma citar o estado ("padrão de venda contra Éden de Baixa"), e prefixá-la com
    // o mesmo nome fazia a frase dizer duas vezes de quem se trata.
    const quem = edenNome(st.eden);
    notes.push(`Storm123 <b>desenhado, mas não operável</b>` +
      `${quem ? ` (${escapeHtml(quem)})` : ""} — ` +
      `${veto ? escapeHtml(veto) : "o filtro Éden veta"}. Por isso o padrão aparece e ` +
      `os níveis (gatilho, alvo, stop) não: eles estão no card, com o motivo inteiro.`);
  }
  if (zones.length) notes.push("Faixas do plano rotuladas na linha do preço — os níveis e a base de cada um ficam no card da análise.");
  if (!notes.length) notes.push("Nenhum setup identificado na janela do gráfico.");
  $("chartNote").innerHTML = notes.map((n) => `<span class="cn-line">${n}</span>`).join("");

  drawPriceChart(cv, chart, cv._actionable);
  bindChartZoom(cv);
  return true;
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
  el.innerHTML = ALL_TFS.map(([tf, curto, completo]) => {
    const on = enabled.has(tf);
    const active = tf === _tf;
    // PENDENTE ≠ ATIVO: o clicado se marca na hora (o clique não se perde), mas
    // quem carrega o realce é o frame que está DESENHADO. Enquanto a resposta não
    // chega, chip ativo, carimbo do gráfico e níveis dizem todos a mesma coisa.
    const pendente = tf === _tfPendente;
    const cls = ["tf-btn", active ? "is-active" : "", pendente ? "is-pendente" : "",
                 on ? "" : "is-off"].filter(Boolean).join(" ");
    const title = pendente
      ? `Recalculando no ${completo}… os níveis na tela ainda são do frame atual`
      : on
      ? `Recalcular no ${completo}`
      : "Frame indisponível para este ativo (o backend não inventa candle)";
    return `<button type="button" class="${cls}" data-tf="${tf}" ${on ? "" : "disabled"} ` +
      `title="${escapeHtml(title)}" aria-label="${escapeHtml(completo)}">${escapeHtml(curto)}</button>`;
  }).join("");
  bindTfSelector();
  renderReevalBtn();
  // A linha da revalidação acompanha o seletor: onde há frame pra trocar, há o
  // interruptor do que acontece ao trocar.
  renderRevalLinha();
}

// Nome humano de uma entrada degradada. O backend SEMPRE manda label (os dois
// produtores são estruturados e a fronteira normaliza registro antigo), então o
// "fonte" aqui é último recurso pra dado irrecuperável — não o caso normal.
function degradedName(d) {
  return escapeHtml((d && (d.label || d.report_key)) || "fonte");
}

// Banner de fonte degradada. Separa as DUAS coisas que o motor reporta pelo mesmo
// canal, porque elas dizem o oposto uma da outra:
//   • kind="missing" — a fonte caiu mesmo após a nova tentativa automática; a
//     análise foi feita SEM ela. Cabe "trate como ausente" e reavaliar com ela.
//   • kind="suspect" — o turno ESTÁ na análise, só saiu com o texto sinalizado
//     pelo verificador de sanidade. Dizer "feito sem" aqui seria mentira.
// Em ambos os casos a fonte é nomeada e o motivo vai na lista. Some quando nada
// degradou.
// A lista degradada da run ABERTA. O banner precisa ser redesenhado quando o frame
// muda (o rótulo do botão nomeia o frame), e o payload do /api/chart não a carrega —
// ela é da ANÁLISE, não do timeframe.
let _openDegraded = null;

function renderDegraded(list) {
  const el = $("degradedBanner");
  if (!el) return;
  _openDegraded = Array.isArray(list) && list.length ? list : null;
  if (!Array.isArray(list) || !list.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const items = list.filter(Boolean);
  const suspect = items.filter((d) => d.kind === "suspect");
  const missing = items.filter((d) => d.kind !== "suspect");
  const heads = [];
  if (missing.length) {
    const plural = missing.length > 1;
    heads.push(
      `<div class="dg-head">Análise feita <b>SEM</b> ${plural ? "as fontes" : "a fonte"}: ` +
      `<b>${missing.map(degradedName).join(" · ")}</b></div>` +
      `<div class="dg-sub">Tentei automaticamente mais uma vez antes de seguir. As leituras acima ` +
      `não incluem ${plural ? "essas fontes" : "essa fonte"} — trate como ausente, não como sinal.</div>`
    );
  }
  if (suspect.length) {
    const plural = suspect.length > 1;
    heads.push(
      `<div class="dg-head">Texto sinalizado ${plural ? "nos turnos" : "no turno"} de: ` +
      `<b>${suspect.map(degradedName).join(" · ")}</b></div>` +
      `<div class="dg-sub">${plural ? "Essas leituras entraram" : "Essa leitura entrou"} na análise — ` +
      `não ${plural ? "foram" : "foi"} descartada. O verificador de sanidade achou sinal de texto ` +
      `corrompido/inventado, então leia ${plural ? "esses trechos" : "esse trecho"} com desconfiança.</div>`
    );
  }
  const reasons = items
    .filter((d) => d.reason)
    .map((d) => `<li><b>${degradedName(d)}</b>: ${escapeHtml(d.reason)}</li>`)
    .join("");
  // Reavaliar faz sentido nos dois casos (a fonte que caiu tende a voltar; o turno
  // sinalizado tende a sair limpo numa nova geração) — só o texto do botão muda.
  //
  // E O RÓTULO DIZ O FRAME. *"Sempre mando revalidar em um timeframe ele tá voltando
  // para o Diário."* Estava certo: este botão rodava no frame do VEREDITO enquanto o
  // outro rodava no da TELA — duas semânticas para a mesma palavra, e o usuário só
  // descobria qual era pelo resultado. Agora os dois seguem o frame que está na tela
  // e AMBOS o nomeiam, então não há o que descobrir.
  const noFrame = ` no ${tfNome(_tf)}`;
  const btnLabel = (missing.length
    ? `⟳ Reavaliar com ${missing.length > 1 ? "essas fontes" : "essa fonte"}`
    : "⟳ Refazer a análise") + noFrame;
  el.innerHTML =
    heads.join("") +
    (reasons ? `<ul class="dg-list">${reasons}</ul>` : "") +
    `<button type="button" class="dg-btn" id="reevalSourcesBtn">${btnLabel}</button>`;
  el.classList.remove("hidden");
  const btn = $("reevalSourcesBtn");
  // Reavaliar = rodar a análise inteira de novo NO FRAME DA TELA: a fonte que caiu
  // por transitório tende a voltar, e o frame é o que o usuário está olhando — não
  // o do veredito anterior. Uma regra só para os dois botões (ver o rótulo acima).
  if (btn) btn.addEventListener("click", () => reevaluate(_tf));
}

// Carimbo do cabeçalho: "veredito no <frame>". Deixa explícito em qual timeframe
// o veredito aberto foi realmente computado (task 012) — some no run com erro.
function renderVerdictTf() {
  const el = $("verdictTf");
  if (!el) return;
  el.textContent = "veredito no " + tfNome(_verdictTf);
  el.classList.remove("hidden");
}

// Botão "reavaliar veredito neste TF": usa o frame ATUAL do gráfico (_tf). Quando
// já é o frame do veredito, fica desabilitado dizendo isso — não há o que refazer.
function renderReevalBtn() {
  const btn = $("reevalBtn");
  if (!btn) return;
  if (!_openTicker) { btn.classList.add("hidden"); return; }
  const label = tfNome(_tf);
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
    progress: { phase: "Inicializando", label: `Reavaliando no ${tfNome(tf)}…`, percent: 2, plan: [], reached: [] },
  });
  apiPost("/api/analyze", { ticker: _openTicker, date: _openDate || "", method, timeframe: tf })
    .then((r) => r.json())
    .then((data) => {
      if (data && data.run_id) {
        rememberRunToken(data.run_id, data.run_token);
        watchRun(data.run_id); loadHistory();
      } else { $("formError").textContent = (data && data.error) || "falha ao reavaliar"; }
    })
    .catch(() => { $("formError").textContent = "falha ao reavaliar"; });
}

// O SELETOR DE CAMADAS. Dois grupos, porque são duas perguntas: quais LEITURAS
// estão traçadas e quais famílias de MÉDIA. Seleção é texto (DA-078 regra 9), e o
// ativo se distingue por cor e peso — mesma gramática do seletor de timeframe.
//
// Fica sempre visível: não é mais "um extra que aparece quando há outra família", é
// o controle do que a tela mostra. As leituras listadas são as que EXISTEM neste
// plano — botão que liga o nada seria promessa falsa.
function renderCamadasSelector(a) {
  const el = $("camadasSelector");
  if (!el) return;
  const leituras = leiturasDisponiveis(a);
  const grupo = (rotulo, itens, ajuda) => itens.length
    ? `<span class="camadas-k" title="${escapeHtml(ajuda)}">${rotulo}</span>` +
      itens.map((f) => {
        const on = camadasAtivas().has(f);
        const nome = CAMADA_NOME_TODAS[f];
        const tit = on
          ? `Esconder ${nome} no gráfico` +
            (CAMADAS_LEITURA.includes(f) ? " — os níveis continuam inteiros no card dele" : "")
          : `Mostrar ${nome} no gráfico` +
            (CAMADAS_LEITURA.includes(f)
              ? "; com duas leituras na tela cada rótulo passa a dizer de qual método é" : "");
        return `<button type="button" class="camada-btn${on ? " is-active" : ""}" ` +
          `data-camada="${f}" aria-pressed="${on}" title="${escapeHtml(tit)}">` +
          `${escapeHtml(nome)}</button>`;
      }).join("")
    : "";
  el.innerHTML =
    grupo("leituras", leituras, "Quais níveis e pontos o gráfico traça") +
    grupo("médias", CAMADAS_MEDIA,
          "Famílias de média: MMS é a do Padrão, EMA 8/21/50 é a do Erick. " +
          "A EMA 80 do Éden acompanha a leitura do Storm.");
  el.classList.toggle("hidden", !el.innerHTML);
  bindCamadasSelector();
}

// LEITURA DISPONÍVEL E DESLIGADA SE ANUNCIA.
//
// *"eu não vi nenhum desenho do storm123 nos gráficos que analisei."* O gráfico passou
// a desenhar só a leitura do método aberto (DA-088) — está certo, foi o que ele pediu.
// O que faltou foi DIZER que a outra existe: numa análise Padrão o Storm não aparecia,
// não havia aviso, e o botão de ligar se confundia com o resto da barra. Do ponto de
// vista dele, o Storm deixou de existir. Trocamos "mistura tudo" por "sumiu e não
// avisou", que é pior — o primeiro ele consegue desfazer.
//
// O aviso fica NO GRÁFICO, acima do canvas, e liga a camada em UM clique. Ele não
// desenha nada sozinho e não é alarme: é o estado "disponível, desligado" dito em voz
// alta. Some quando tudo que existe já está na tela.
function renderCamadasAviso(a) {
  const el = $("camadasAviso");
  if (!el) return;
  const fora = leiturasDisponiveis(a).filter((f) => !camadaVisivel(f));
  if (!fora.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const nomes = fora.map((f) => CAMADA_NOME_TODAS[f]);
  const quantos = fora.length > 1
    ? `${nomes.join(" e ")} têm leitura` : `${nomes[0]} tem leitura`;
  el.innerHTML =
    `<span class="cav-txt">${escapeHtml(quantos)} para este ativo e ` +
    `${fora.length > 1 ? "não estão desenhadas" : "não está desenhada"} no gráfico.</span>` +
    // Classe PRÓPRIA (não `camada-btn`): o botão da barra é um interruptor que também
    // desliga; este é uma chamada pra ação, e um `querySelectorAll('.camada-btn')`
    // passaria a ver dois botões da mesma camada — o mesmo `data-camada` já basta pro
    // ouvinte compartilhado.
    fora.map((f) => `<button type="button" class="cav-btn" data-camada="${f}" ` +
      `title="Desenhar ${escapeHtml(CAMADA_NOME_TODAS[f])} no gráfico">` +
      `mostrar ${escapeHtml(CAMADA_NOME_TODAS[f])}</button>`).join("");
  el.classList.remove("hidden");
  bindCamadasSelector(el);
}

function bindCamadasSelector(alvo) {
  // O MESMO ouvinte serve à barra e ao aviso: os dois disparam a mesma ação (ligar/
  // desligar a camada), e duplicar a lógica faria o botão do aviso divergir do da
  // barra na primeira mudança.
  const el = alvo || $("camadasSelector");
  if (!el || el._bound) return;
  el._bound = true;
  el.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-camada]");
    if (!btn) return;
    const f = btn.dataset.camada;
    const ativas = camadasAtivas();
    if (ativas.has(f)) ativas.delete(f); else ativas.add(f);
    // A partir do primeiro toque a escolha é DELE, e vale nas próximas análises.
    _camadasTocado = true;
    salvaCamadas();
    // redesenha com o estado do canvas (o plano e o chart moram nele)
    const cv = $("priceChart");
    if (cv && cv._chart) renderChartCard(cv._chart, _openTicker, cv._actionable, cv._tf);
  });
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
  el.textContent = msg;
  el.classList.remove("hidden");
}
function hideDegrade() {
  const el = $("chartDegrade");
  if (el) { el.textContent = ""; el.classList.add("hidden"); }
}

// ─────────── REVALIDAÇÃO AUTOMÁTICA AO TROCAR DE FRAME (task 013) ────────────
//
// *"Se durante a mudança do tempo gráfico houver atualização de preço, quero a
// revalidação automática."*
//
// O DEFEITO CONCRETO: `switchTimeframe` recalculava os NÍVEIS a cada troca, mas a
// COTAÇÃO ficava congelada no momento em que a run foi desenhada — o `/api/chart`
// não devolve preço live, e o código caía em `_openLive`. Resultado: o usuário
// trocava de frame cinco vezes ao longo de dez minutos e a tira do cabeçalho
// continuava mostrando o mesmo "cotação agora", com a DISTÂNCIA até o preço da
// análise calculada contra um número velho. A tela afirmava "agora" sobre um
// instante que já tinha passado.
//
// Agora a troca dispara a busca da cotação FRESCA e, quando o preço andou o bastante
// desde a análise, DIZ que revalidou e de quando é a leitura.
//
// FORA DO CAMINHO DA TROCA, e isto foi medido: a primeira versão pedia níveis e
// cotação num `Promise.all`, e com isso a ação primária (trocar de frame) passou a
// esperar o enriquecimento dela — o teste da escada quebrou por causa disso. A troca
// desenha com o que tem; o preço entra por cima quando chega. Se nunca chegar, a
// tela fica exatamente como era antes desta entrega, sem anunciar nada.
//
// O LIMIAR é 0,5%, e não é número novo: é o mesmo `_GATILHO_TOL` que o scanner usa
// para dizer que o preço "chegou no gatilho" (scanner.py: *"0,5% absorve o ruído
// intradiário de um toque iminente"*). Se 0,5% é o que separa ruído de movimento
// para acionar um trade, é o mesmo corte para dizer que o preço da análise
// envelheceu. Um número só no produto para a mesma pergunta — e PROVISÓRIO pelo
// mesmo motivo que lá: a calibrar com o track record.
const REVAL_LIMIAR = 0.005;

// Janela em que a cotação buscada aqui é reaproveitada sem ir à rede. Igual ao TTL
// do servidor (`_PRICE_TTL`, ~45s): trocar de chip cinco vezes seguidas não pode
// virar cinco requisições, e o número já está certo pelos 45s de qualquer jeito.
// É a primeira das TRÊS travas contra cascata — as outras duas são a busca em voo
// compartilhada (`_revalVoo`, pros toques que chegam antes da primeira resposta) e
// o selo `_tfSeq`, que faz só a troca VENCEDORA pintar o que voltou.
const REVAL_COTACAO_TTL_MS = 45000;


const REVAL_CHAVE = "td_reval_auto";
let _revalCota = null;      // {quando, dado} — memo da cotação fresca (já resolvida)
let _revalVoo = null;       // a busca EM VOO, compartilhada por quem chegar durante ela
let _revalNota = null;      // {tf, hora, driftTxt} — o que a tela deve anunciar

// Ligada por padrão, e a escolha dele fica: quem desliga não quer buscar cotação a
// cada troca, e no desligado a tela volta a se comportar como antes (a cotação da
// run, congelada) — dito no title, nunca descoberto pelo resultado.
function revalLigado() {
  try { return localStorage.getItem(REVAL_CHAVE) !== "0"; } catch (e) { return true; }
}

function setRevalLigado(v) {
  try { localStorage.setItem(REVAL_CHAVE, v ? "1" : "0"); } catch (e) { /* modo privado */ }
}

// A cotação vale como ATUAL só numa run de HOJE (DA-073): numa análise de data
// passada o preço de agora não pertence àquela leitura, então não se busca nada e
// não se revalida nada. Fail-open: qualquer erro devolve null e a troca de frame
// segue com o que já tinha — a revalidação nunca pode quebrar a troca.
function cotacaoFresca() {
  if (!_openTicker) return Promise.resolve(null);
  const hoje = _todayManaus || "";
  if (hoje && _openDate && _openDate !== hoje) return Promise.resolve(null);
  const agora = Date.now();
  if (_revalCota && (agora - _revalCota.quando) < REVAL_COTACAO_TTL_MS) {
    return Promise.resolve(_revalCota.dado);
  }
  // O TTL sozinho não segura a cascata: ele só existe DEPOIS que a resposta chega, e
  // três toques em meio segundo acontecem todos ANTES disso — três buscas para a
  // mesma pergunta. Quem chega com uma busca em voo entra NELA. É o mesmo antídoto
  // do selo `_tfSeq` visto do outro lado: lá se descarta a resposta de uma troca
  // superada, aqui não se chega a pedir duas vezes o que já está sendo pedido.
  if (_revalVoo) return _revalVoo;
  const voo = (async () => {
    try {
      const res = await fetch("/api/prices?tickers=" + encodeURIComponent(_openTicker));
      const data = await res.json();
      const p = ((data || {}).prices || {})[_openTicker.toUpperCase()] || null;
      // O carimbo do DIA é o que deixa a tira tratar a cotação como atual (o mesmo que
      // `_cotacao_da_run` grava no backend). Sem ele a unidade sumiria da tela.
      const dado = p && p.price != null ? Object.assign({}, p, { em: hoje }) : null;
      _revalCota = { quando: Date.now(), dado };
      return dado;
    } catch (e) {
      return null;               // fail-open: a troca de frame acontece sem cotação
    } finally {
      _revalVoo = null;
    }
  })();
  _revalVoo = voo;
  return voo;
}

// A linha do controle: o interruptor e, quando houve, o que a revalidação encontrou.
// O usuário TEM que perceber que revalidou — silêncio faz ele achar que a tela
// travou, e pior: faz um número novo parecer o mesmo de antes.
function renderRevalLinha() {
  const el = $("revalLinha");
  if (!el) return;
  if (!_openTicker) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const on = revalLigado();
  const tit = on
    ? "Ao trocar de tempo gráfico, busca a cotação de agora e recalcula os níveis "
      + "daquele frame contra ela. Desligado, a tela usa a cotação de quando a "
      + "análise foi feita (o comportamento antigo)."
    : "Desligada: ao trocar de frame a tela mostra a cotação de quando a análise "
      + "foi feita, e a distância até o preço da análise não se atualiza.";
  const nota = (on && _revalNota)
    ? `<span class="rv-nota">revalidado no <b>${escapeHtml(tfNome(_revalNota.tf))}</b> ` +
      `às <b>${escapeHtml(_revalNota.hora)}</b> — o preço andou ` +
      `<b>${escapeHtml(_revalNota.driftTxt)}</b> desde a análise</span>`
    : "";
  el.innerHTML =
    `<button type="button" class="rv-btn${on ? " is-on" : ""}" id="revalToggle" ` +
    `aria-pressed="${on}" title="${escapeHtml(tit)}">` +
    `revalidar sozinho ao trocar de frame: <b>${on ? "ligado" : "desligado"}</b></button>` + nota;
  el.classList.remove("hidden");
  const b = $("revalToggle");
  if (b) b.addEventListener("click", () => {
    setRevalLigado(!revalLigado());
    _revalNota = null;      // a nota descreve uma revalidação que aconteceu; desligar não a preserva
    renderRevalLinha();
  });
}

// Decide e REGISTRA a revalidação de uma troca que vingou. Devolve a cotação a usar
// na tira (a fresca, ou a da run quando não há fresca).
function revalidaAoTrocar(tf, cota, actionable) {
  if (!revalLigado() || !cota || cota.price == null) return _openLive;
  const base = actionable && actionable.price;
  if (!base) { _openLive = cota; return cota; }
  const drift = (cota.price - base) / base;
  // Abaixo do limiar não há o que anunciar: a tira do cabeçalho já mostra a
  // distância, e um aviso que aparece em toda troca é aviso que ninguém lê.
  _revalNota = Math.abs(drift) >= REVAL_LIMIAR
    ? { tf, hora: horaCurta(), driftTxt: (drift > 0 ? "+" : "−") + pctBR(Math.abs(drift) * 100) + "%" }
    : null;
  _openLive = cota;          // a run aberta passa a carregar a cotação nova
  return cota;
}

// A revalidação DEPOIS da troca: chega quando chegar, e só pinta se a troca que a
// pediu ainda for a da tela (o mesmo selo `_tfSeq` que descarta níveis superados —
// três toques rápidos revalidam UMA vez, no frame que vingou). Sem cotação nova não
// se repinta nada: repintar o mesmo número faria a tela piscar sem dizer nada.
function aplicaRevalidacao(selo, tf, actionable) {
  if (!revalLigado()) return;
  cotacaoFresca().then((cota) => {
    if (selo !== _tfSeq || !cota) return;
    renderHeadPrice(actionable, revalidaAoTrocar(tf, cota, actionable));
    renderRevalLinha();
  });
}

function horaCurta() {
  return new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

// TROCA DE FRAME É ATÔMICA: o realce só se move quando os NÍVEIS chegam.
//
// Antes o `_tf` mudava no clique e o seletor se repintava na hora, enquanto o
// gráfico e os cards continuavam mostrando o frame ANTERIOR até a resposta chegar.
// Nessa janela a tela afirmava uma coisa falsa: chip no "D", carimbo do gráfico no
// "4h" e stop 497,59 — quando o stop do diário era 526,92. Trinta pontos de
// diferença num nível que se opera, apresentados como se fossem daquele frame.
//
// Agora o clicado fica PENDENTE (marcado, pra o clique não parecer perdido) e o
// ATIVO continua sendo o frame que está de fato desenhado. Em nenhum instante o
// par (chip ativo, carimbo, níveis) discorda.
//
// E a resposta ATRASADA de uma troca superada é descartada pelo selo `_tfSeq`:
// clicar D e logo 1h fazia a resposta do D, se chegasse depois, pintar o diário
// por cima do 1h já selecionado — a mesma incoerência por outra porta.
async function switchTimeframe(tf) {
  if (!_openTicker) return;
  // A GUARDA olha o que está DESENHADO, não só o que `_tf` diz. Quando o frame
  // pedido volta sem velas, `_tf` passava a valer o novo e o canvas continuava no
  // antigo; o clique seguinte no MESMO frame caía neste return e não fazia nada —
  // o "às vezes mudo o timeframe e o gráfico não muda", com o clique morrendo em
  // silêncio. Reclicar tem de tentar de novo enquanto a tela não estiver naquele
  // frame (DA-118).
  if (tf === _tf && tf === _tfDesenhado && !_tfPendente) return;
  return carregaFrame(tf, {});
}

// REVALIDAÇÃO: o MESMO caminho da troca, sem apagar nada e sem anunciar-se alto.
// Um segundo caminho de recarga divergiria do primeiro — foi assim que o gráfico
// ganhou um jeito de sumir que a troca não tinha.
async function revalidaFrame(motivo) {
  if (!_openTicker || _revalEmVoo) return;   // não se empilha revalidação
  return carregaFrame(_tf, { revalidacao: true, motivo: motivo || "" });
}

// O caminho ÚNICO de carga do frame. `opts.revalidacao` muda só a apresentação:
// a troca ANUNCIA ("Recalculando no 4h…", chip pendente) porque é uma ação que o
// usuário acabou de pedir; a revalidação é silenciosa e EM LUGAR, porque ninguém
// pediu nada e o que está na tela continua válido até o novo chegar.
async function carregaFrame(tf, opts) {
  const reval = !!(opts && opts.revalidacao);
  const selo = ++_tfSeq;
  let notaAnterior = null;
  if (reval) _revalEmVoo = true;
  const note = $("chartNote");
  if (!reval) {
    _tfPendente = tf;
    // A nota nomeia o frame em que se revalidou. Sobrevivendo à troca ela passaria a
    // apontar um frame que não está mais na tela — a próxima revalidação a reescreve.
    _revalNota = null;
    renderTfSelector();                       // marca o clicado como pendente
    renderEscada(_escada);                    // e o degrau clicado também
    hideDegrade();
    // A nota do gráfico descreve o DESENHO. Enquanto a carga corre ela vira
    // "Recalculando…"; se a carga falhar, deixá-la assim faria a tela afirmar para
    // sempre um recálculo que já terminou (mal). Guarda-se o texto pra devolver.
    if (note) { notaAnterior = note.innerHTML; note.textContent = `Recalculando no ${tfNome(tf)}…`; }
  } else {
    marcaRevalidando(true);
  }
  const encerra = () => {
    if (reval) { _revalEmVoo = false; marcaRevalidando(false); }
    if (selo === _tfSeq && !reval) { _tfPendente = null; renderTfSelector(); renderEscada(_escada); }
    // O desenho não mudou: a nota dele volta a ser o que era.
    if (note && notaAnterior !== null) note.innerHTML = notaAnterior;
  };
  try {
    const q = new URLSearchParams({ ticker: _openTicker, date: _openDate || "", tf, method: _openMethod || "padrao" });
    const res = await fetch("/api/chart?" + q.toString());
    const data = await res.json();
    if (selo !== _tfSeq) { if (reval) { _revalEmVoo = false; marcaRevalidando(false); } return; }
    if (!res.ok || data.error) {
      encerra();
      // O ERRO PASSA A TER NOME. Antes tudo virava "Falha ao recalcular timeframe."
      // e a causa morria no catch — por isso o sintoma chegava como "não muda" em
      // vez de uma mensagem que dissesse o quê.
      avisaFalhaDeFrame(tf, data.error || `o servidor respondeu ${res.status}`, reval);
      // Uma falha não desliga a revalidação automática: o próximo candle continua
      // fechando, e desistir na primeira recusa da fonte deixaria a tela parada
      // até alguém clicar em alguma coisa.
      agendaProximaRevalidacao();
      return;
    }
    // O backend pode ter caído pro diário (fonte intradiária fora do ar); o
    // frame realmente exibido vem de data.timeframe, nunca uma barra inventada.
    _tf = data.timeframe || tf;
    _tfPendente = null;
    if (Array.isArray(data.timeframes) && data.timeframes.length) _timeframes = data.timeframes;
    renderTfSelector();
    // A COTAÇÃO não é do frame — é do ativo AGORA. O /api/chart não a devolve, e
    // passar `undefined` fazia a unidade "último fechamento 465,58" sumir da tira ao
    // trocar de frame e voltar ao trocar de novo. Ela é lembrada da run aberta, e a
    // busca fresca (logo abaixo) a substitui QUANDO chegar — nunca antes.
    renderHeadPrice(data.actionable, data.live_price || _openLive);
    renderRevalLinha();
    // O rótulo do "reavaliar com essa fonte" NOMEIA o frame da tela — trocar de
    // frame sem redesenhar o banner deixaria ele prometendo o frame anterior.
    if (_openDegraded) renderDegraded(_openDegraded);
    renderSetupCards(data.actionable);
    // A escada NÃO se recalcula ao trocar de frame — as cinco leituras são as
    // mesmas. O que muda é qual degrau está ABERTO, e essa marca é diferente da
    // do veredito: "o que estou vendo" e "o que decidiu" são duas coisas.
    renderEscada(_escada);
    // O gráfico DIZ se conseguiu desenhar. Sem velas o desenho anterior FICA (não
    // se apaga o que já se sabe), e aí a tela tem de declarar que ele é de outro
    // frame — senão ela afirma o frame novo mostrando o desenho velho.
    const desenhou = renderChartCard(data.price_chart, _openTicker, data.actionable, _tf);
    if (desenhou) _tfDesenhado = _tf;
    // Resposta boa que não trouxe velas: `renderChartCard` sai antes de reescrever
    // a nota, e o "Recalculando no 1h…" ficaria na tela para sempre — afirmando um
    // recálculo que já terminou. A nota descreve o desenho, e o desenho é o antigo.
    if (!desenhou && note && notaAnterior !== null) note.innerHTML = notaAnterior;
    declaraFrameDoGrafico(_tf, desenhou);
    carregaExecCard();          // outro frame, outro plano: o card acompanha
    if (data.degraded && data.notice) showDegrade(data.notice);
    // E SÓ AGORA a cotação, FORA do caminho da troca. Medido: pendurar a troca na
    // busca de preço (um `Promise.all` com o /api/chart) fez o frame demorar a
    // trocar — a ação primária esperando o enriquecimento dela. A troca desenha com
    // o que tem; quando o preço chega, a tira e a nota se atualizam por cima.
    aplicaRevalidacao(selo, _tf, data.actionable);
    if (reval) { _revalEmVoo = false; marcaRevalidando(false); }
    agendaProximaRevalidacao();   // o próximo fechamento de candle DESTE frame
  } catch (err) {
    if (selo !== _tfSeq) { if (reval) { _revalEmVoo = false; marcaRevalidando(false); } return; }
    encerra();
    // A CAUSA vai pra tela. "Falha ao recalcular timeframe." sem o motivo é o que
    // fazia o defeito chegar como "às vezes não muda": a tela não mentia, ela
    // simplesmente não contava.
    avisaFalhaDeFrame(tf, (err && err.message) || String(err), reval);
    agendaProximaRevalidacao();   // idem: a falha não desliga o relógio
  }
}

// A tela nunca mente sobre QUAL frame está desenhado. Quando a carga do frame novo
// não trouxe velas, o desenho anterior fica — e esta linha diz de qual frame ele é.
function declaraFrameDoGrafico(tfPedido, desenhou) {
  const el = $("chartFrameAviso");
  if (!el) return;
  const mostra = !desenhou && _tfDesenhado && _tfDesenhado !== tfPedido;
  el.classList.toggle("hidden", !mostra);
  el.innerHTML = mostra
    ? `o gráfico continua no <b>${escapeHtml(tfNome(_tfDesenhado))}</b> — o ` +
      `<b>${escapeHtml(tfNome(tfPedido))}</b> não voltou com velas. Os níveis abaixo ` +
      `já são do ${escapeHtml(tfNome(tfPedido))}.`
    : "";
}

// A falha ganha NOME e lugar fixo. Numa troca ela é ruidosa (o usuário pediu e não
// recebeu); numa revalidação automática é discreta (ninguém pediu), mas nunca
// silenciosa — silêncio foi o que transformou um erro em "às vezes não funciona".
function avisaFalhaDeFrame(tf, causa, reval) {
  const el = $("chartFrameAviso");
  if (el) {
    el.classList.remove("hidden");
    el.innerHTML = reval
      ? `a revalidação automática do <b>${escapeHtml(tfNome(tf))}</b> falhou ` +
        `(${escapeHtml(causa)}) — o que está na tela continua sendo a leitura anterior.`
      : `não deu pra carregar o <b>${escapeHtml(tfNome(tf))}</b> (${escapeHtml(causa)}) — ` +
        `a tela continua no <b>${escapeHtml(tfNome(_tfDesenhado || _tf))}</b>. Clique de novo pra tentar.`;
  }
  // eslint-disable-next-line no-console
  console.warn("[frame]", tf, causa);
}

// Indicador DISCRETO de revalidação em curso: opacidade no card, como o scan faz
// durante a varredura (task 014). Sem spinner cobrindo o gráfico, sem apagar nada.
function marcaRevalidando(on) {
  const card = $("chartCard");
  if (card) card.classList.toggle("is-revalidando", !!on);
}


// ============ REVALIDAÇÃO AUTOMÁTICA POR FECHAMENTO DE CANDLE (DA-118) ========
//
// Pedido do Samyr: *"as revalidações no diário, 4h e 1h devem acontecer em cada
// fechamento de timeframe respectivo e deve ser automático"*.
//
// **O relógio é do SERVIDOR, e é o mesmo do scan.** `agenda.py` já calcula o
// próximo fechamento (cadência pelo candle + atraso pós-fechamento) e a passada
// agendada do scan roda por ele. Recalcular esse horário em JavaScript criaria um
// segundo agendador com regra própria, e no dia em que as duas divergissem
// ninguém saberia qual manda. A tela PERGUNTA (`/api/agenda/proxima`).
//
// **Ação fora do pregão não revalida à toa** — a mesma regra de
// `agenda.alvos_da_passada`, respondida pelo servidor no campo `revalida`: cripto
// sempre, ação só com a sessão ativa. Fora disso o candle não anda e a chamada
// seria gasto sem informação.
//
// **Custo: $0.** A revalidação chama `/api/chart`, que é plano determinístico —
// nenhum LLM entra neste caminho.
//
// **Aba em segundo plano:** o navegador afrouxa `setTimeout` (e no mobile chega a
// parar). Por isso o horário-alvo é guardado em `_revalAlvoMs` e checado ao voltar
// pro primeiro plano: se já passou, revalida na hora e reagenda. O timer é o
// caminho feliz; a volta da aba é a rede de segurança — mesma disciplina do
// `onVisibleForeground` que já existia pro progresso e pros preços.
let _revalAlvoMs = 0;
// Selo do agendamento, na mesma disciplina do `_tfSeq`: trocar de frame duas vezes
// depressa deixa duas perguntas de horário no ar, e a MAIS ANTIGA pode responder
// por último — armando um timer que a mais nova já tinha decidido não armar. Só o
// último pedido pode mexer no timer.
let _revalAgendaSeq = 0;

function cancelaRevalidacaoAgendada() {
  if (_revalTimer) { clearTimeout(_revalTimer); _revalTimer = null; }
  _revalAlvoMs = 0;
}

async function agendaProximaRevalidacao() {
  const selo = ++_revalAgendaSeq;
  cancelaRevalidacaoAgendada();
  if (!_openTicker || !_tf) return;
  try {
    const q = new URLSearchParams({ tf: _tf, ticker: _openTicker, asset_type: _assetType || "" });
    const res = await fetch("/api/agenda/proxima?" + q.toString());
    if (selo !== _revalAgendaSeq || !res.ok) return;
    const a = await res.json();
    if (selo !== _revalAgendaSeq) return;               // pergunta superada: é lixo
    if (!a || !a.revalida || !a.em_segundos) return;   // pregão fechado: não insiste
    const ms = Math.max(1000, Number(a.em_segundos) * 1000);
    _revalAlvoMs = Date.now() + ms;
    _revalTimer = setTimeout(() => { _revalTimer = null; disparaRevalidacaoDoCandle(); }, ms);
  } catch (e) { /* sem agenda a tela só não se atualiza sozinha — nada quebra */ }
}

function disparaRevalidacaoDoCandle() {
  _revalAlvoMs = 0;
  // Aba escondida: não gasta chamada agora. Ao voltar, `onVisibleForeground` vê o
  // alvo vencido e revalida — melhor uma revalidação na volta do que um punhado
  // acumulado que o navegador soltou de uma vez.
  if (document.visibilityState !== "visible") { _revalAlvoMs = 1; return; }
  revalidaFrame("fechamento de candle").finally(() => agendaProximaRevalidacao());
}

// Chamado pelo `onVisibleForeground`: se o fechamento passou enquanto a aba estava
// atrás, revalida agora. Se não passou, só garante que o timer existe (o navegador
// pode tê-lo matado).
function revalidaSeOCandleFechouEnquantoEuNaoOlhava() {
  if (!_openTicker) return;
  const venceu = _revalAlvoMs && Date.now() >= _revalAlvoMs;
  if (venceu) { cancelaRevalidacaoAgendada(); disparaRevalidacaoDoCandle(); return; }
  if (!_revalTimer) agendaProximaRevalidacao();
}

// ────────────────────────── UM GRÁFICO, UM MÉTODO ───────────────────────────
//
// "Percebo tbm que mistura tudo em um gráfico só, Storm123, Setup123 e Padrão com
// Erick." Estava certo, e eram TRÊS misturas empilhadas na mesma tela:
//
//   1. as MÉDIAS — as duas famílias eram desenhadas sempre, pra todo método: MMS
//      20/50/200 (Padrão) mais EMA 8/21/50 (Erick), mais a EMA 80 do Éden nas runs
//      do Storm. Sete linhas, das quais o método aberto usa três;
//   2. os NÍVEIS — numa run do Storm o gráfico traçava os do Storm E os do plano
//      (Setup123 + recuo à média), porque a única condição era o Storm ter opinião.
//      Daí os "dois stops empilhados" a 0,39 um do outro, sem nada dizendo que são
//      de famílias diferentes;
//   3. os PONTOS numerados — os círculos 1-2-3 vêm do detector de SWINGS, mesmo
//      numa run do Storm, cujo 1-2-3 é outro (três candles). Mesma numeração, pontos
//      diferentes: a colisão que o comentário do módulo já declarava.
//
// A regra: **o gráfico desenha a leitura que dá NOME ao método aberto.** As outras
// leituras continuam INTEIRAS nos cards (DA-077 — uma leitura, um card); no gráfico
// só entram se pedidas, e aí vêm nomeadas. E quando duas famílias dividem a tela,
// TODO rótulo carrega a sua — "stop (SL)" vira "Setup123 · stop (SL)" ao lado de
// "Storm123 · stop (SL)", porque dois níveis do mesmo papel sem dono é o defeito.
const CAMADA_DO_METODO = {
  padrao: "plano", erick: "plano", setup123: "plano", storm123: "storm",
};
const CAMADA_NOME = { plano: "Setup123", storm: "Storm123" };
// A média é parte da leitura, não enfeite: o Éden É a MME 8 × MME 80, e o recuo do
// Padrão é a MMS. Ligar uma camada traz as médias que a justificam.
const MEDIAS_DA_CAMADA = {
  plano: { padrao: ["20", "50", "200"], erick: [], setup123: ["20", "50", "200"] },
  storm: { ema: ["8", "80"] },
};
// AS CAMADAS SÃO DO USUÁRIO. "Eu deveria poder selecionar a camada do que eu quero
// ver, no time frame que eu quiser" — então o gráfico abre na leitura do método
// (ninguém deve precisar configurar nada pra ver o próprio resultado) e a partir
// dali quem manda é ele, em qualquer frame.
//
// Duas famílias de camada, porque são duas perguntas diferentes:
//   • LEITURAS — quais níveis/pontos estão traçados (plano × Storm);
//   • MÉDIAS   — quais famílias de média (MMS do Padrão × EMA do Erick). Elas vêm
//     no payload sempre, então ligar/desligar é decisão de tela, não de backend.
// A EMA 80 acompanha a leitura do Storm: ela é METADE do filtro Éden, e um Éden sem
// a lenta na tela é um veto que não se confere.
//
// PERSISTÊNCIA POR SESSÃO: enquanto ele NÃO tocou no seletor, cada análise abre na
// camada do seu método — que é o comportamento certo pra quem só quer ver o
// resultado. Depois do primeiro toque a escolha DELE passa a valer nas análises
// seguintes; obrigar a reconfigurar a cada uma seria transformar preferência em
// tarefa repetida. Some ao fechar a aba (sessionStorage), não vira config global.
const CAMADAS_LEITURA = ["plano", "storm"];
const CAMADAS_MEDIA = ["mms", "emas"];
const CAMADA_NOME_TODAS = {
  plano: "Setup123", storm: "Storm123", mms: "MMS (Padrão)", emas: "EMA (Erick)",
};
// Médias que cada família de média traça, e a do Éden que anda com o Storm.
const JANELAS_DA_MEDIA = { mms: { ma: ["20", "50", "200"] }, emas: { ema: ["8", "21", "50"] } };
const _CHAVE_CAMADAS = "td.camadas.v1";

// `null` = ainda não inicializado. É diferente de um conjunto VAZIO, que significa
// "ele desligou tudo" e tem de ser respeitado. Sem a distinção, um gráfico desenhado
// fora do fluxo de abrir análise (o CONFRONTO, por exemplo) sairia sem faixa nenhuma
// e sem legenda — o estado nasceria vazio e ninguém o teria inicializado.
let _camadas = null;
let _camadasTocado = false;

// O padrão de ABERTURA de um método: a leitura dele e a família de média dele.
function camadasPadrao(metodo) {
  const leitura = CAMADA_DO_METODO[metodo] || "plano";
  const medias = metodo === "erick" ? ["emas"] : metodo === "storm123" ? [] : ["mms"];
  return new Set([leitura, ...medias]);
}

function camadaDoMetodo() {
  return CAMADA_DO_METODO[_openMethod] || "plano";
}

// O conjunto ATIVO, com inicialização preguiçosa: qualquer desenho fora do fluxo de
// abrir análise cai no padrão do método em vez de num gráfico em branco.
function camadasAtivas() {
  if (_camadas === null) _camadas = camadasPadrao(_openMethod);
  return _camadas;
}

function camadaVisivel(familia) {
  return camadasAtivas().has(familia);
}

function salvaCamadas() {
  try {
    sessionStorage.setItem(_CHAVE_CAMADAS,
      JSON.stringify({ tocado: _camadasTocado, camadas: [...camadasAtivas()] }));
  } catch (e) { /* aba privada / storage bloqueado: a sessão só não lembra */ }
}

function carregaCamadas() {
  try {
    const raw = sessionStorage.getItem(_CHAVE_CAMADAS);
    if (!raw) return null;
    const o = JSON.parse(raw);
    if (!o || !Array.isArray(o.camadas)) return null;
    return { tocado: !!o.tocado, camadas: new Set(o.camadas) };
  } catch (e) { return null; }
}

// Chamado ao ABRIR uma análise. Sem toque do usuário, segue o método. Com toque,
// mantém a escolha dele — mas nunca deixa o gráfico VAZIO: se a preferência não
// acender nenhuma leitura que exista neste plano, a do método volta como chão.
// Gráfico em branco não é liberdade, é defeito.
function iniciaCamadas(a) {
  const salvo = carregaCamadas();
  _camadasTocado = !!(salvo && salvo.tocado);
  _camadas = _camadasTocado ? new Set(salvo.camadas) : camadasPadrao(_openMethod);
  const existem = camadasDisponiveis(a);
  // O chão é a camada do MÉTODO — a não ser que ela não tenha o que desenhar (Storm
  // vetado pelo Éden, por exemplo). Aí o chão é a primeira que EXISTE: "nunca deixar o
  // gráfico vazio" era a intenção desta linha, e ligar uma camada sem desenho deixava
  // o gráfico igualmente vazio, só que sem ninguém dizer por quê.
  if (!existem.some((f) => _camadas.has(f))) {
    const doMetodo = camadaDoMetodo();
    _camadas.add(existem.includes(doMetodo) ? doMetodo : existem[0]);
  }
  salvaCamadas();
}

// O R:R QUE O GRÁFICO CARIMBA É O DA LEITURA DESENHADA. Ele saía sempre de
// `a.risk_reward` — o do plano —, então numa run do Storm o chip mostrava o número
// de uma leitura que não estava traçada em lugar nenhum da tela. É a mesma mistura
// que esta task veio desfazer, só que num carimbo em vez de numa linha.
//
// Com as duas famílias na tela o número ganha DONO no próprio texto; sozinha, fica
// limpo. O Storm tem duas entradas: leva a ANTECIPADA (a que o preço alcança
// primeiro) com o nome dela — as duas continuam inteiras no card.
function rrDoGrafico(a) {
  if (!a) return { rr: null, prefixo: "", morto: false };
  const duas = familiasNaTela(a).length > 1;
  // PADRÃO MORTO NÃO CARIMBA R:R. O chip é a razão que decide se o setup vale o
  // risco — carimbá-lo sobre um padrão invalidado é oferecer a conta de um trade que
  // não existe mais, o mesmo defeito que a DA-091 tirou do gatilho e da pílula. Aqui
  // não basta devolver vazio: se a leitura desenhada morreu, o chip DIZ isso (gráfico
  // sem chip é indistinguível de gráfico sem setup).
  let morto = false, vetado = false;
  if (camadaVisivel("plano") && a.risk_reward) {
    if (ehFantasma(a.pattern)) morto = true;
    else return { rr: a.risk_reward, prefixo: duas ? "Setup123 " : "", morto: false };
  }
  // Storm VETADO desenha o padrão mas não tem R:R operável — e o chip não pode calar,
  // pelo mesmo motivo do morto: gráfico sem chip é indistinguível de gráfico sem setup.
  let edenNomeVeto = "", edenCurtoVeto = "";
  if (camadaVisivel("storm") && stormEstado(a.storm) === "vetado") {
    vetado = true;
    const edenSt = (a.storm || {}).eden;
    const dirSt = (a.storm.pattern || {}).direction;
    edenNomeVeto = edenContraste(edenSt, dirSt, edenNome(edenSt));
    edenCurtoVeto = edenContraste(edenSt, dirSt, edenCurto(edenSt));
  }
  if (camadaVisivel("storm") && a.storm && a.storm.opera === true) {
    if (ehFantasma(a.storm.pattern)) {
      morto = true;
    } else {
      const ls = (a.storm.leituras || []).slice().sort(
        (x, y) => (x.ordem === "confirmada" ? 1 : 0) - (y.ordem === "confirmada" ? 1 : 0));
      const L = ls.find((x) => x.risk_reward);
      if (L) {
        const n = L.entrada === "ponto3" ? "p3" : L.entrada === "ponto2" ? "p2" : "p2/3";
        return { rr: L.risk_reward, prefixo: `${duas ? "Storm123 " : ""}${n} `, morto: false };
      }
    }
  }
  return { rr: null, prefixo: "", morto, vetado,
           edenNome: edenNomeVeto, edenCurto: edenCurtoVeto };
}

// As leituras DESENHADAS que morreram. É o que explica um gráfico com padrão na tela
// e sem nível nenhum — sem isto, a nota dizia "Nenhum setup identificado" sobre um
// gráfico que tem três pontos numerados em cinza, o que é falso.
function fantasmasNaTela(chart, a) {
  const nomes = [];
  const pat = chart && chart.markers && chart.markers.pattern_123;
  if (camadaVisivel("plano") && ehFantasma(pat)) nomes.push("Setup123");
  if (camadaVisivel("storm") && a && stormEstado(a.storm) === "invalidado") nomes.push("Storm123");
  return nomes;
}

// O STORM VETADO NA NOTA. Ele está desenhado e sem nível nenhum — sem uma linha
// dizendo por quê, a nota cairia no "Nenhum setup identificado" sobre três pontos
// numerados na vela, que é a contradição que esta task veio desfazer.
function stormVetadoNaTela(a) {
  return !!(camadaVisivel("storm") && a && stormEstado(a.storm) === "vetado");
}

// As camadas que EXISTEM neste plano — só se oferece o que há pra mostrar.
function camadasDisponiveis(a) {
  const fam = [];
  // Camada que não tem nível nenhum não é camada: oferecê-la seria um botão que
  // liga o nada, e a legenda diria que há algo desenhado onde não há.
  if (a && (a.pattern || a.buy_zone || a.stop || a.target || a.realize_zone)) fam.push("plano");
  // BASTA HAVER PADRÃO. A camada exigiu `opera === true` por algumas horas, quando o
  // desenho também exigia — e a consequência era o pior dos mundos: o card descrevendo
  // "Storm123 de compra · NÃO OPERA" enquanto nem a camada existia pra procurar. Agora
  // o padrão vetado É DESENHADO (com o veto escrito na vela), então a camada dele tem
  // o que ligar. O que o veto continua tirando são os NÍVEIS, não a figura.
  if (a && a.storm && a.storm.pattern) fam.push("storm");
  return fam.length ? fam : ["plano"];
}

// Quantas famílias estão na tela agora. Com mais de uma, todo rótulo se identifica.
function familiasNaTela(a) {
  return camadasDisponiveis(a).filter(camadaVisivel);
}

// As camadas de LEITURA que este plano tem pra oferecer (as de média existem sempre).
function leiturasDisponiveis(a) {
  return camadasDisponiveis(a).filter((f) => CAMADAS_LEITURA.includes(f));
}

function nomeiaTag(tag, familia, precisa) {
  return precisa ? `${CAMADA_NOME[familia]} · ${tag}` : tag;
}

// Médias que o gráfico desenha: as famílias LIGADAS, mais a lenta do Éden quando a
// leitura do Storm está na tela (ela é metade do filtro — sem ela o veto não se
// confere). Ligar e desligar é decisão de tela: as duas famílias vêm no payload.
function mediasVisiveis(a) {
  const ma = new Set(), ema = new Set();
  // No CONFRONTO as duas famílias aparecem sempre: comparar Padrão × Erick é o
  // objetivo declarado daquela tela, e esconder a média de uma das colunas seria
  // tirar do confronto justamente o que ele confronta.
  if (_openView === "compare") {
    ["20", "50", "200"].forEach((w) => ma.add(w));
    ["8", "21", "50"].forEach((w) => ema.add(w));
    return { ma, ema };
  }
  const ativas = camadasAtivas();
  CAMADAS_MEDIA.forEach((f) => {
    if (!ativas.has(f)) return;
    (JANELAS_DA_MEDIA[f].ma || []).forEach((w) => ma.add(w));
    (JANELAS_DA_MEDIA[f].ema || []).forEach((w) => ema.add(w));
  });
  if (camadaVisivel("storm") && a && a.storm) {
    (MEDIAS_DA_CAMADA.storm.ema || []).forEach((w) => ema.add(w));
  }
  return { ma, ema };
}

// O frame EXIBIDO é o que produziu o veredito? Só quando os dois são conhecidos e
// DIFERENTES a leitura é exploratória — frame desconhecido não vira acusação.
//
// Isto não é cosmético: cada frame recalcula o plano INTEIRO (a mesma ação, no
// mesmo dia, saía com stop 207,00 no 1h e 175,09 no diário). São trades distintos,
// e só um deles é o da decisão. O outro serve pra olhar a estrutura, não pra operar.
function ehExploratorio(tf) {
  // Na COMPARAÇÃO isto não vale: lá são duas runs, cada uma com o SEU veredito no
  // SEU frame, e `_verdictTf` guarda o da coluna A. Carimbar a B de "exploratória"
  // seria afirmar que ela não decidiu nada — o oposto do que ela é.
  if (_openView === "compare") return false;
  const f = tf || _tf;
  return !!(f && _verdictTf && f !== _verdictTf);
}

// The plan's operable zones, ready to draw ON the chart (price on the band edge),
// de-duplicated: in "aguardar recuo" the buy zone and the pullback are the same
// rising average, so only one green band is drawn. A trigger-point pullback is a
// line the 1-2-3 already draws, so it is dropped here. Empty when no plan/levels.
function planZones(a) {
  if (!a) return [];
  const out = [];
  // Duas famílias na tela ⇒ todo rótulo diz de quem é. Uma só ⇒ rótulo limpo, que é
  // o caso normal e não merece prefixo repetido em cada linha.
  const marcar = familiasNaTela(a).length > 1;
  const vePlano = camadaVisivel("plano");
  // Sem a camada do plano, o gráfico é o do Storm — se ELA estiver ligada. Com as
  // duas desligadas o gráfico fica só com as velas, e é isso mesmo: ele pediu pra
  // não ver nível nenhum. (O chão que impede a ABERTURA vazia é o `iniciaCamadas`.)
  if (!vePlano) return camadaVisivel("storm") ? planZonesStorm(a, out, marcar) : out;
  // A zona da média é o setup do RECUO — NUNCA sai rotulada só "compra", porque
  // o 1-2-3 de compra é OUTRO setup, com outro gatilho, desenhado no mesmo gráfico.
  // Era essa colisão de nome que fazia o ZEC-USD 4h parecer contradição: a faixa
  // dizia "compra 803,09" com o preço em 835,20 e o card dizia COMPRA — dois
  // setups, um nome só. O rótulo rico já vem do backend (``tag``).
  const buy = a.buy_zone;
  if (buy && buy.price != null) {
    // "não ativa agora" é fato do backend (preço fora da faixa DESENHADA), não
    // inferência da tela: faixa fora do preço para de se desenhar como entrada.
    const fora = buy.active_now === false;
    const nome = buy.tag || "recuo à média";
    // A tag CURTA existe pro rótulo desenhado dentro do gráfico: no telefone a
    // largura útil é ~300px e "recuo à média (MMS50) — não ativa agora 806,67"
    // atravessava a régua do eixo e saía cortado (task 020). A curta diz o mesmo
    // em menos letra; a longa continua na legenda, que tem a linha inteira.
    const curto = `recuo ${buy.ma_label || "média"}`;
    out.push({ ...buy, color: fora ? ZONE_COLORS.inativa : ZONE_COLORS.buy,
               inactive: fora, familia: "plano",
               tag: nomeiaTag(fora ? `${nome} — não ativa agora` : nome, "plano", marcar),
               // a etiqueta CURTA é a que o gráfico estreito desenha: se ela não se
               // nomeia, o telefone volta a ter faixa anônima com duas famílias
               tagCurto: nomeiaTag(fora ? `${curto} (inativa)` : curto, "plano", marcar) });
  }
  // A região de realização só se chama "alvo" quando de fato é: num setup de VENDA
  // ela é o topo acima (resistência, nunca o alvo de um short) e, quando coincide
  // com o gatilho do 1-2-3, a linha do próprio padrão já a desenha — não se traça
  // o mesmo nível duas vezes. O backend carimba esse papel em ``role``.
  const rz = a.realize_zone;
  if (rz && rz.price != null && rz.role !== "gatilho") {
    const rzColor = rz.role === "resistencia" ? ZONE_COLORS.resist : ZONE_COLORS.realize;
    out.push({ ...rz, color: rzColor, familia: "plano",
               tag: nomeiaTag(rz.role_label || "realização (alvo)", "plano", marcar),
               tagCurto: nomeiaTag(rz.role === "resistencia" ? "resistência" : "realização",
                                   "plano", marcar) });
  }
  // Alvo (TP) do padrão. Mesmo nível da realização → NÃO desenha um segundo: a
  // faixa que já está lá passa a dizer que ela é o alvo.
  const tg = a.target;
  if (tg && tg.price != null) {
    const twin = tg.same_as_realize && out.find((z) => z.color === ZONE_COLORS.realize && z.price === tg.price);
    if (twin) {
      twin.tag = nomeiaTag("realização = alvo (TP)", "plano", marcar);
      twin.tagCurto = nomeiaTag("alvo", "plano", marcar); twin.color = ZONE_COLORS.target;
    } else {
      out.push({ ...tg, color: ZONE_COLORS.target, familia: "plano",
                 tag: nomeiaTag("alvo (TP)", "plano", marcar),
                 tagCurto: nomeiaTag("alvo", "plano", marcar) });
    }
  }
  // Invalidação e stop são LINHAS (nível exato), não faixas: a invalidação é o
  // ponto 3 da série e o stop é ela com a folga de ATR declarada pelo backend.
  const inv = a.invalidation;
  if (inv && inv.price != null) {
    out.push({ label: inv.label, price: inv.price, low: null, high: null,
               color: ZONE_COLORS.invalid, familia: "plano",
               tag: nomeiaTag("invalidação", "plano", marcar), dash: [2, 3] });
  }
  const st = a.stop;
  if (st && st.price != null) {
    out.push({ label: st.label, price: st.price, low: null, high: null,
               color: ZONE_COLORS.stop, familia: "plano",
               tag: nomeiaTag("stop (SL)", "plano", marcar),
               tagCurto: nomeiaTag("stop", "plano", marcar), dash: [6, 4] });
  }
  // NÍVEIS DO STORM — outra leitura, outra cor, e o nome dela no rótulo: nunca se
  // confundem com os do 1-2-3 deste módulo, que estão no mesmo gráfico com números
  // diferentes por construção.
  //
  // Só quando o Éden AUTORIZA. Setup vetado não ganha traço no gráfico: o gráfico é
  // a figura operável, e desenhar níveis de um trade que a regra proíbe é convidar a
  // operá-lo. Nada se perde — o card do Storm continua com cada número e com o
  // motivo do veto escrito.
  if (camadaVisivel("storm")) planZonesStorm(a, out, marcar);
  // A FAIXA DO PONTO 3 — a "preparação para acompanhar a hora de entrar". Ela só
  // existe quando o padrão está em gestação ou morreu; com padrão vivo o ponto 3 já
  // está desenhado, e repetir a espera seria dizer que falta o que já existe.
  // Cor NEUTRA de propósito: é uma faixa de ESPERA, não um nível operável — pintá-la
  // com o verde de compra faria dela um convite a entrar antes do setup existir.
  const pj = a.projecao_p3;
  if (pj && pj.low != null && pj.high != null) {
    out.push({ ...pj, color: ZONE_COLORS.projecao, familia: "plano", inactive: true,
               tag: nomeiaTag(`onde o ponto 3 precisa nascer (${pj.direcao})`, "plano", marcar),
               tagCurto: nomeiaTag("ponto 3 a formar", "plano", marcar) });
  }
  const pb = a.pullback_zone;
  const buyPrice = buy && buy.price;
  const isBand = pb && pb.low != null && pb.high != null;
  // só desenha o recuo separado quando é uma FAIXA distinta da compra (não o
  // gatilho-ponto do 1-2-3, que a própria marcação do padrão já traça)
  if (pb && pb.price != null && isBand && pb.price !== buyPrice) {
    out.push({ ...pb, color: ZONE_COLORS.pullback, familia: "plano",
               tag: nomeiaTag("recuo a aguardar", "plano", marcar) });
  }
  return out;
}

// NÍVEIS DO STORM — outra leitura, outra cor, e o nome dela no rótulo. Só quando o
// Éden AUTORIZA: setup vetado não ganha traço no gráfico, porque o gráfico é a
// figura operável e desenhar níveis de um trade que a regra proíbe é convidar a
// operá-lo. Nada se perde — o card do Storm continua com cada número e com o veto
// escrito.
function planZonesStorm(a, out, marcar) {
  const storm = a.storm;
  if (!storm || storm.opera !== true || !storm.pattern) return out;
  // PADRÃO MORTO NÃO TEM NÍVEL OPERÁVEL. Gatilho, alvo e stop do Storm saem TODOS do
  // padrão (o gatilho é a perda do extremo do ponto 2, o alvo é a amplitude dele
  // projetada, o stop é o próprio ponto 2 com folga): quando o padrão é invalidado,
  // os três descrevem um trade que não existe mais. Desenhá-los seria a mesma
  // armadilha da DA-091 — o gatilho de um setup extinto na tela é o pior nível que
  // ela pode ter. Os três pontos continuam desenhados em fantasma, e o card do Storm
  // continua com cada número e com a data da invalidação escrita.
  if (ehFantasma(storm.pattern)) return out;
  // O prefixo é o NOME do método quando as duas famílias dividem a tela, e a forma
  // curta de sempre quando o Storm está sozinho — prefixo repetido em cada linha de
  // um gráfico que só tem Storm é ruído, não informação.
  const pre = marcar ? "Storm123" : "Storm";
  // COR POR PAPEL, IGUAL AO PLANO. O stop do Storm saía azul ao lado do stop do
  // plano em vermelho, na MESMA tela: o nível onde se perde dinheiro pintado com a
  // cor de "pertence ao Storm". O alvo tinha o mesmo defeito ao lado do alvo verde
  // do plano. O gatilho continua na cor do Storm porque ele não é ganho nem perda —
  // é a ENTRADA, e é o nível que o losango desenhado na vela representa; deixá-lo
  // azul mantém o vínculo visível entre o padrão e o preço que o aciona.
  const stLinha = (price, tag, curto, color) => {
    if (price == null) return;
    out.push({ label: tag, price, low: null, high: null, familia: "storm",
               color, tag, tagCurto: curto, dash: TRACO_STORM });
  };
  // O stop é UM (comum às duas entradas); gatilho e alvo são de CADA leitura, e por
  // isso o rótulo diz de qual — dois "Storm · gatilho" no mesmo gráfico seriam dois
  // níveis com o mesmo nome, que é o defeito da DA-075.
  stLinha((storm.stop || {}).price, `${pre} · stop (SL)`, `${pre} SL`, ZONE_COLORS.stop);
  (storm.leituras || []).forEach((L) => {
    const n = L.entrada === "ponto3" ? "p3" : L.entrada === "ponto2" ? "p2" : "p2/3";
    stLinha(L.trigger, `${pre} ${n} · gatilho`, `${pre} ${n} gat.`, ZONE_COLORS.storm);
    stLinha((L.target || {}).price, `${pre} ${n} · alvo (TP)`, `${pre} ${n} TP`,
            ZONE_COLORS.target);
  });
  return out;
}

// Percentual com UMA casa, em pt-BR. A tela inteira escreve "218,56"; o acerto de
// equilíbrio saía "88.5%" com ponto — o único número da tela falando outro idioma.
function pctBR(v) {
  return Number(v).toFixed(1).replace(".", ",");
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

// ── O 1-2-3 DESENHADO NA VELA, para as duas leituras ────────────────────────
// Linha tracejada ligando os três pontos, marcador numerado em cada um COM O PREÇO
// ao lado, e a linha do gatilho quando ela existe. O que a família decide: a cor, a
// FORMA do marcador (círculo/losango), o quanto ele se afasta da vela e o nome.
//
// FANTASMA: padrão morto perde opacidade, veste o cinza e perde o que convida a
// operar — a linha do gatilho. Continua na tela porque a história explica onde o
// preço está, mas para de competir com o que ainda vale (DA-091).
function desenha123(ctx, g, cfg, saida, rotulos) {
  const { pts, cor, forma, dash, fantasma, vetado, estado, nome, mostraNome,
          trigger, dist, familia } = cfg;
  if (!pts || !pts.length) return;
  const { x, y, padL, plotW } = g;
  ctx.save();
  // PESO = ESTADO, mas peso é CONTRASTE e não opacidade nominal. O vetado guarda a cor
  // do método e por isso 0,7 ainda o deixa forte na tela; o morto já perdeu a cor, e
  // multiplicá-lo por 0,45 sobre o preto o levava abaixo do piso de legibilidade —
  // "não existe mais" tinha virado "não está aqui" (ver ALFA_FANTASMA). A ordem de
  // leitura continua a mesma, medida no pixel: vivo > vetado > morto.
  if (fantasma) ctx.globalAlpha = ALFA_FANTASMA;
  else if (vetado) ctx.globalAlpha = 0.7;
  ctx.strokeStyle = cor; ctx.setLineDash(dash); ctx.lineWidth = 1.5;
  ctx.beginPath();
  pts.forEach((p, k) => { const px = x(p.i), py = y(p.price); k ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
  ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
  if (trigger != null && !fantasma) {
    const ty = y(trigger);
    ctx.strokeStyle = cor + "80"; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(padL, ty); ctx.lineTo(padL + plotW, ty); ctx.stroke();
    ctx.setLineDash([]);
  }
  pts.forEach((p) => {
    const px = x(p.i), cy = y(p.price), lado = p.kind === "L" ? 1 : -1;
    const my = cy + lado * dist;
    ctx.font = "bold 12px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = "#000000";
    ctx.beginPath();
    if (forma === "losango") {
      ctx.moveTo(px, my - 9.5); ctx.lineTo(px + 9.5, my);
      ctx.lineTo(px, my + 9.5); ctx.lineTo(px - 9.5, my); ctx.closePath();
    } else {
      ctx.arc(px, my, 8, 0, Math.PI * 2);
    }
    ctx.fill(); ctx.strokeStyle = cor;
    // Contorno TRACEJADO no vetado: um anel quebrado se lê como "não vale operar"
    // antes de qualquer palavra, e não gasta cor nova (DA-078 regra 3).
    if (vetado) ctx.setLineDash([3, 2]);
    // ANEL MAIS GROSSO NO MORTO. Um traço diagonal de 1px rende com cobertura PARCIAL
    // por pixel na antialiasing, e o losango do fantasma media contraste pior que o
    // número ao lado — que tem área sólida de glifo. Dois pixels devolvem a cobertura
    // sem trocar a cor: a forma é portadora, e engrossá-la não devolve o morto ao
    // vocabulário dos vivos.
    if (fantasma) ctx.lineWidth = 2;
    ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
    ctx.fillStyle = cor; ctx.fillText(p.lab, px, my);
    // O MARCADOR É OBSTÁCULO pro texto: sem isto o respaldo de um preço pousava em
    // cima do número de outro ponto e tampava justamente o que a task veio marcar.
    if (rotulos) rotulos.obstaculos.push({ x: px - 10, y: my, w: 20, h: 20 });
    // O PREÇO ao lado do número — sem ele o marcador diz que ali houve um ponto, mas
    // não a QUE altura, que é justamente o número usado pra montar a ordem. Vai pra
    // fila junto com as etiquetas: com duas famílias na tela, preço de uma caía sob
    // etiqueta da outra, e o número desaparecia.
    const preco = fmtNum(p.price);
    rotulos.push({ x: px, y: my + lado * 16, text: preco, align: "centro", pilula: false,
                   cor: fantasma ? COR_FANTASMA : "#8b97ad",
                   opaco: fantasma ? ALFA_FANTASMA : (vetado ? 0.7 : 1) });
    // O QUE SEPARA DESENHADO DE VISÍVEL, nas duas maneiras de um desenho não chegar
    // aos olhos. `naVista`: com zoom, um ponto antigo cai centenas de pixels à
    // esquerda do plot e continua sendo "pintado" — o canvas não recorta, então o
    // comando ocorre e nada aparece. `px`/`py` (em pixels de CSS): ONDE ele foi
    // pintado, pra que a suíte possa ir ao pixel e medir o CONTRASTE do que saiu, em
    // vez de só afirmar que o desenho foi pedido. Sem as duas, a telemetria declara
    // três pontos na tela enquanto a tela está vazia — e a suíte fica verde por cima
    // do defeito que o usuário está olhando.
    if (saida) saida.push({ familia, nome, lab: p.lab, preco, forma, cor,
                            fantasma: !!fantasma, vetado: !!vetado,
                            px: Math.round(px), py: Math.round(my),
                            naVista: px >= padL && px <= padL + plotW,
                            estado: estado || (fantasma ? "invalidado" : "") });
  });
  // Etiquetas ao lado do PRIMEIRO ponto: o nome da família (só quando há mais de uma
  // na tela — prefixo repetido num gráfico de uma leitura só é ruído) e "invalidado".
  // NÃO são pintadas aqui: vão pra fila e saem por último, POR CIMA de tudo e com
  // fundo opaco. Pintadas em linha, a etiqueta de uma família caía sobre o PREÇO de
  // um ponto da outra e as duas viravam uma palavra só ("Storm123462,00") — a
  // confusão que esta tela existe pra não ter.
  const p0 = pts[0], lado0 = p0.kind === "L" ? 1 : -1;
  const base = y(p0.price) + lado0 * dist;
  const etiquetas = [];
  if (mostraNome) etiquetas.push(nome);
  // O ESTADO, ESCRITO. "invalidado" e "não opera (Éden)" são a mesma família de aviso:
  // o desenho está na tela, e a palavra diz o que fazer com ele.
  if (estado) etiquetas.push(estado);
  else if (fantasma) etiquetas.push("invalidado");
  // NA COLUNA DO PONTO 1, logo depois do preço dele — não ao LADO do marcador. Ao
  // lado, a pílula (≈60px) cruzava a coluna dos pontos vizinhos e a de-colisão a
  // empurrava 90px pra baixo: um selo "Storm123" solto no meio do gráfico não nomeia
  // padrão nenhum. Na coluna, ela desce no máximo o que a própria coluna ocupa, e as
  // duas famílias se separam sozinhas quando os marcadores apontam pra lados opostos.
  const peso = fantasma ? 0.45 : (vetado ? 0.7 : 1);
  etiquetas.forEach((t, k) => rotulos.push(
    { x: x(p0.i), y: base + lado0 * (32 + k * 15), text: t, cor, align: "centro",
      pilula: true, opaco: peso }));
  ctx.restore();
}

// PREÇOS E ETIQUETAS DO 1-2-3, por último e sem se cobrirem.
//
// Pintados em linha, dentro do laço de cada família, o preço de um ponto de uma
// leitura caía sob a etiqueta da outra e as duas viravam uma palavra só
// ("Storm123462,00"). Aqui eles saem depois de TODOS os marcadores, com de-colisão
// vertical: quem cai sobre um já colocado desce 13px até sair de cima dele. Um número
// deslocado ainda se lê; um número coberto some — e era o preço do ponto, que é o
// número usado pra montar a ordem.
const _FONTE_ROTULO = { pilula: "bold 10px ui-monospace, Menlo, monospace",
                        texto: "10px ui-monospace, Menlo, monospace" };

function pintaRotulos123(ctx, rotulos, limites) {
  if (!rotulos.length) return [];
  ctx.save();
  ctx.textBaseline = "middle";
  // Começa pelos MARCADORES: eles já estão pintados e não se movem, então entram
  // como caixas ocupadas. O que se move é o texto.
  const caixas = (rotulos.obstaculos || []).slice();
  const ALT = 15;
  // AS ETIQUETAS DE FAMÍLIA SE ACOMODAM PRIMEIRO. Quem entra depois é quem se desloca,
  // e uma pílula "Storm123" empurrada 100px pra longe do ponto 1 deixa de nomear o
  // padrão — vira um selo solto no meio do gráfico. O preço deslocado continua na
  // coluna do seu marcador; o nome, não.
  const fila = rotulos.slice().sort((a, b) => (b.pilula ? 1 : 0) - (a.pilula ? 1 : 0));
  // O SLOT LIVRE MAIS PRÓXIMO, alternando abaixo e acima do lugar natural. Empurrar
  // sempre PRA BAIXO empilhava seis preços (duas famílias × três pontos) numa coluna
  // só no telefone, e o último saía do gráfico por cima da régua de datas. Busca
  // finita e limitada ao plot: sem candidato legível, o rótulo NÃO é pintado — número
  // sobreposto não se lê, e o card carrega cada um deles por escrito.
  const passos = [0];
  for (let k = 1; k <= 12; k++) passos.push(k * ALT, -k * ALT);
  const pintados = [];
  // A GEOMETRIA do que saiu, ao lado do texto. `pintados` é lido como lista de
  // strings pela suíte inteira, então o onde viaja numa propriedade à parte (o
  // JSON de um array ignora props) e sai em `dataset.rotulos123Geo`: é o que
  // permite ir ao PIXEL do preço e medir se ele se lê, em vez de confiar que sim.
  const geo = [];
  fila.forEach((r) => {
    ctx.font = r.pilula ? _FONTE_ROTULO.pilula : _FONTE_ROTULO.texto;
    const w = ctx.measureText(r.text).width + (r.pilula ? 10 : 4);
    const x0 = r.align === "centro" ? r.x - w / 2 : r.x - 4;
    const livre = (yy) => !caixas.some(
      (o) => x0 < o.x + o.w && o.x < x0 + w && Math.abs(yy - o.y) < (o.h + ALT) / 2 - 0.5);
    // ALCANCE: um preço só vale colado no SEU marcador. Deslocado meia tela ele deixa
    // de dizer quanto vale aquele ponto e vira um número solto — pior que ausente,
    // porque o olho o atribui ao ponto errado. Passando do alcance, não se pinta: o
    // card carrega cada número por escrito, e a régua carrega os níveis. A pílula da
    // família tem alcance maior (é uma por leitura, e nomear é o trabalho dela).
    // 3 passos ≈ 45px: cabe o lado OPOSTO do próprio marcador (o preço "por cima" em
    // vez de "por baixo", 32px de distância), que é o primeiro lugar a tentar quando o
    // natural está ocupado. Com 2 passos o ponto 2 — o EXTREMO do movimento — perdia o
    // preço por 2px de folga.
    const alcance = (r.pilula ? 5 : 3) * ALT;
    let yy = null;
    for (const d of passos) {
      if (Math.abs(d) > alcance) break;
      const cand = r.y + d;
      if (cand < limites.topo || cand > limites.base) continue;
      if (livre(cand)) { yy = cand; break; }
    }
    if (yy == null) return;
    caixas.push({ x: x0, y: yy, w, h: ALT });
    pintados.push(r.text);
    geo.push({ text: r.text, x: x0, y: yy, w, cor: r.cor, opaco: r.opaco });
    ctx.globalAlpha = r.opaco;
    // Fundo escuro em AMBOS: a etiqueta da família leva pílula com borda (ela nomeia
    // a leitura), o preço leva só o respaldo. Sem ele, "474,00" sobre pavio verde e
    // média tracejada some no telefone, onde a área útil é ~250px e os três candles
    // do Storm caem quase no mesmo x.
    roundRect(ctx, x0, yy - 7.5, w, 15, 3);
    ctx.globalAlpha = r.opaco * (r.pilula ? 0.92 : 0.78);
    ctx.fillStyle = "#0b0b0b"; ctx.fill();
    ctx.globalAlpha = r.opaco;
    if (r.pilula) {
      ctx.strokeStyle = r.cor + "88"; ctx.lineWidth = 1; ctx.stroke();
    }
    ctx.fillStyle = r.cor; ctx.textAlign = "left";
    ctx.fillText(r.text, x0 + (r.pilula ? 5 : 2), yy + 0.5);
  });
  ctx.restore();
  pintados.geo = geo;
  return pintados;
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
  // a escala vertical cresce pelo que está DESENHADO — incluir os pontos de uma
  // camada escondida achataria o gráfico por causa de níveis que ninguém vê
  if (pat && camadaVisivel("plano")) [pat.p1.price, pat.p2.price, pat.p3.price, pat.trigger].forEach(grow);
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
  // A pílula é do que está DESENHADO. Com a camada do plano desligada o gatilho do
  // 1-2-3 de swings continuava no eixo, sozinho, sem a linha nem os pontos que o
  // explicam — um preço operável de uma leitura que o usuário mandou sumir.
  if (pat && camadaVisivel("plano")) {
    // sem pílula de gatilho num padrão morto: o eixo é onde o olho procura preço
    // operável, e pôr ali o gatilho de um setup extinto é convidar a operá-lo
    if (!ehFantasma(pat)) {
      axisPills.push({ y: y(pat.trigger), text: fmtAxis(pat.trigger), bg: patColor(pat), fg: "#000000" });
    }
  }
  layoutAxisPills(axisPills, padT + pillH / 2 + 1, padT + plotH - pillH / 2 - 1, pillH + 2);
  // As pílulas do eixo ficam OBSERVÁVEIS (mesmo padrão do dataset.levelLabels): é o
  // lugar onde o olho procura preço operável, e "o gatilho de um padrão morto não
  // aparece aqui" é regra de tela — regra que não se mede volta sozinha.
  canvas.dataset.axisPills = JSON.stringify(axisPills.map((p) => p.text));
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

  // OS CARIMBOS SÃO DESENHADOS POR ÚLTIMO (ver a chamada, depois das velas).
  // Eles ficam no canto superior esquerdo, que é onde as velas chegam quando o preço
  // está no topo da janela — e velas desenhadas DEPOIS passavam por cima do texto.
  // A definição fica aqui, junto do carimbo do eixo a que ela pertence; só a ordem
  // de execução muda.
  const desenharCarimbos = () => {
    // timeframe stamp — o frame do padrão fica escrito NO gráfico (não só no card),
    // pra ninguém confundir um 1-2-3 de 15m com o do diário.
    //
    // E ele diz se o frame é o DO VEREDITO. Trocar o chip de tempo trocava o plano
    // inteiro — mesmo ativo, mesmo dia, SL de 207,00 no 1h e 175,09 no diário — e os
    // três saíam pintados igual, como se os três fossem operáveis. O frame que não
    // decidiu nada é EXPLORATÓRIO, e agora está escrito em cima do gráfico, não só
    // num carimbo lá no topo da página que sai da tela quando se rola até aqui.
    const exploratorio = ehExploratorio(chart.timeframe);
    const tfText = (tfNome(chart.timeframe) || "Diário")
      + (exploratorio ? "  ·  exploratório" : "");
    ctx.font = "bold 11px ui-monospace, Menlo, monospace";
    const tfW = ctx.measureText(tfText).width + 14;
    roundRect(ctx, padL + 2, padT + 2, tfW, 17, 4);
    ctx.fillStyle = "#0d0d0d"; ctx.fill();
    ctx.strokeStyle = exploratorio ? "rgba(255,255,255,0.35)" : "rgba(255,255,255,0.12)";
    ctx.lineWidth = 1; ctx.stroke();
    ctx.fillStyle = exploratorio ? "#e6e9ef" : "#cdd6e4";
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(tfText, padL + 9, padT + 2 + 8.5);
    canvas.dataset.tf = tfText;
    // Onde o carimbo TERMINA, em px de CSS a partir da borda do canvas. É o que
    // permite provar que a dica de zoom (HTML, por cima) não cai em cima dele.
    canvas.dataset.carimboFim = String(Math.round(padL + 2 + tfW));

    // R:R do setup DENTRO do gráfico, colado no carimbo do frame — é a razão que
    // decide se o 1-2-3 vale o risco, então não pode ficar só num card ao lado.
    //
    // A COR SEGUE O NÚMERO, e só ela: VERDE exclusivamente quando o retorno supera o
    // risco. Abaixo de 1 o chip sai BRANCO com a conta escrita ao lado — "R:R 0,21:1"
    // em verde afirmava o contrário do que o número diz, e verde é o vocabulário de
    // "pode ir" na tela inteira (print do Samyr, 29/08, diário). Nem vermelho: o
    // setup existe, quem está desfavorável é a conta (DA-078 regra 3 — aviso é
    // palavra, não cor nova).
    //
    // E o fundo é OPACO. Com 0,85 de alfa a faixa verde do alvo atravessava o chip
    // por baixo e o tingia — a cor certa no texto e a errada atrás dele.
    const _rrG = rrDoGrafico(a);
    const rrPlan = _rrG.rr;
    const _pre = _rrG.prefixo;
    const rrTem = rrPlan && rrPlan.rr != null;
    // Sem número a linha NÃO desaparece: ela diz que não é calculável. Um gráfico sem
    // chip nenhum é indistinguível de um sem setup, e era assim que o R:R aparecia só
    // no diário enquanto 1h e 4h ficavam mudos.
    //
    // No telefone o plot útil é ~250px e a frase inteira passava por cima da régua de
    // preço. Em vez de cortar (perder letra) ou encolher a fonte (ilegível), o texto
    // DEGRADA por medida: cai pra forma mais curta que couber. A conta nunca se perde
    // de vez — o card logo abaixo carrega "risco > retorno (4,8x)" sempre.
    // Com o padrão ACIONADO, o que explica o número baixo é o PERCURSO, não o
    // múltiplo do risco — "andou 91% do caminho" diz por que sobrou pouco; "risco
    // 11x o retorno" só repete que é pouco. O percurso vem primeiro quando existe.
    const andou = rrTem && rrPlan.andado_pct != null
      ? `andou ${Math.round(rrPlan.andado_pct)}%` : "";
    const rrOpcoes = rrTem
      ? (andou
          ? [`R:R ${_pre}${fmtNum(rrPlan.rr)}:1 · ${andou} do caminho`,
             `R:R ${_pre}${fmtNum(rrPlan.rr)}:1 · ${andou}`,
             `R:R ${_pre}${fmtNum(rrPlan.rr)}:1`]
          : rrRuim(rrPlan.rr)
          ? [`R:R ${_pre}${fmtNum(rrPlan.rr)}:1 · risco ${rrVezes(rrPlan.rr)}x o retorno`,
             `R:R ${_pre}${fmtNum(rrPlan.rr)}:1 · risco ${rrVezes(rrPlan.rr)}x`,
             `R:R ${_pre}${fmtNum(rrPlan.rr)}:1`]
          : [`R:R ${_pre}${fmtNum(rrPlan.rr)}:1`])
      : (rrPlan ? [`R:R não calculável — ${motivoCurto(rrPlan.note)}`,
                   "R:R não calculável", "R:R sem base"]
                : _rrG.morto ? ["R:R não vale — padrão invalidado", "R:R — invalidado"]
                // ESCADA de três degraus, e o NOME sobrevive aos dois primeiros: no
                // telefone o chip cabe em ~250px, e "Éden de Baixa" × "armadilha" são
                // vetos diferentes — só o primeiro se resolve esperando. Encolhe a
                // frase, não o dado (DA-101).
                : _rrG.vetado ? [`R:R não vale — ${_rrG.edenNome || "o Éden"} veta este setup`,
                                 `R:R — ${_rrG.edenCurto || "Éden"} veta`,
                                 "R:R — Éden veta"]
                : []);
    ctx.font = "bold 11px ui-monospace, Menlo, monospace";
    const rrText = rrOpcoes.find((t) => ctx.measureText(t).width + 14 <= plotW)
      || rrOpcoes[rrOpcoes.length - 1] || "";
    canvas.dataset.rr = rrText;
    canvas.dataset.rrCor = "";
    if (rrText) {
      // o chip verde diz "o retorno supera o risco" — é ganho, e ganho tem UMA cor
      // na tela (ZONE_COLORS.buy == --green). Era o terceiro literal do verde do
      // alvo solto no arquivo (task 20260831-005).
      const rrCol = rrTem && !rrRuim(rrPlan.rr) ? ZONE_COLORS.buy : "#e6e9ef";
      // A cor do chip fica OBSERVÁVEL (mesmo padrão do dataset.levelLabels): "verde só
      // quando o retorno supera o risco" é regra de tela, e regra de tela que não se
      // mede volta sozinha na próxima mudança de layout.
      canvas.dataset.rrCor = rrCol;
      ctx.font = "bold 11px ui-monospace, Menlo, monospace";
      const rrW = ctx.measureText(rrText).width + 14;
      const rrY = padT + 23;   // 2ª linha: a 1ª divide espaço com a dica de zoom (HTML)
      roundRect(ctx, padL + 2, rrY, rrW, 17, 4);
      ctx.fillStyle = "#0d0d0d"; ctx.fill();
      ctx.strokeStyle = rrCol + "88"; ctx.lineWidth = 1; ctx.stroke();
      ctx.fillStyle = rrCol; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(rrText, padL + 9, rrY + 8.5);
    }
  };

  // plan zones: translucent bands BEHIND the candles (edge labels drawn on top later)
  zones.forEach((z) => {
    const hasBand = z.low != null && z.high != null && z.high > z.low;
    if (hasBand) {
      const yTop = y(z.high), yBot = y(z.low);
      // Faixa NÃO ATIVA (preço fora dela) se desenha diferente da ativa: mais
      // apagada e com a borda tracejada. Antes as duas saíam idênticas, e uma
      // região que não é entrada agora tinha o mesmo peso visual de uma que é.
      ctx.fillStyle = z.color + (z.inactive ? "10" : "1f");
      ctx.fillRect(padL, yTop, plotW, Math.max(2, yBot - yTop));
      ctx.strokeStyle = z.color + (z.inactive ? "40" : "55"); ctx.lineWidth = 1;
      if (z.inactive) ctx.setLineDash([4, 3]);
      ctx.strokeRect(padL + 0.5, yTop + 0.5, plotW - 1, Math.max(2, yBot - yTop));
      ctx.setLineDash([]);
    } else {
      const yy = y(z.price);
      ctx.strokeStyle = z.color + "aa"; ctx.setLineDash(z.dash || [5, 4]); ctx.lineWidth = 1.2;
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

  // MÉDIAS DO MÉTODO ABERTO, e só. As duas famílias eram desenhadas sempre — MMS
  // 20/50/200 do Padrão MAIS EMA 8/21/50 do Erick, mais a EMA 80 do Éden nas runs
  // do Storm: sete linhas numa tela onde o método usa três. A média é parte da
  // LEITURA (o Éden É a MME 8 × MME 80), então ela acompanha a camada, e ligar uma
  // camada extra traz as médias que a justificam.
  const _med = mediasVisiveis(a);
  Object.entries(chart.ema || {}).forEach(([w, arr]) => {
    if (!_med.ema.has(String(w))) return;
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
    if (!_med.ma.has(String(w))) return;
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

  // Agora sim os carimbos, POR CIMA das velas e das médias (ver a definição acima).
  desenharCarimbos();

  // date -> index map for markers
  const idx = {}; candles.forEach((c, i) => { idx[c.d] = i; });

  // buy-region dots (at the candle low)
  //
  // A NOTA CONTAVA A LISTA; A TELA DESENHAVA O QUE COUBE. A linha abaixo do gráfico diz
  // "N região(ões) de recuo à média marcada(s) no período" a partir de
  // ``markers.buy_regions``, e este laço pulava em SILÊNCIO toda marca cujo candle não
  // está no período carregado deste tempo gráfico — ou que o zoom empurrou pra fora.
  // No print da task 048 a nota prometia 12 e havia UMA bolinha na tela. É a mesma
  // lição da DA-107, na outra marca: nunca sumir em silêncio.
  const regions = (chart.markers && chart.markers.buy_regions) || [];
  const _marcas = [], _marcasPint = [];
  regions.forEach((r) => {
    const i = idx[r.date];
    _marcas.push({ lab: "marca", i, d: r.date });
    if (i == null) return;
    const px = x(i), py = y(candles[i].l) + 7;
    _marcasPint.push({ naVista: px >= padL && px <= padL + plotW });
    ctx.fillStyle = "#2ecc71";
    ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2); ctx.fill();
    // ANEL ESCURO: o verde da bolinha é o MESMO pixel do candle de alta (ambos
    // "#2ecc71", DA-078 regra de cor por PAPEL colidindo consigo mesma — um marcador
    // histórico e a direção do candle não são o mesmo significado). Sem contorno, a
    // bolinha some dentro do pavio de qualquer candle verde por perto — cor não
    // muda (ainda é ESTE verde), mas ganha um portador que a cor sozinha não tem.
    ctx.strokeStyle = "#0b0b0b"; ctx.lineWidth = 1.25;
    ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2); ctx.stroke();
  });

  // 1-2-3 NA VELA — as DUAS leituras, pelo mesmo desenhador.
  //
  // Eram dois blocos quase iguais, e por serem dois divergiram: o do Storm ficou sem
  // o preço ao lado do ponto e sem o tratamento de fantasma. O método que o Samyr
  // mais usa era o único cujos três pontos não diziam quanto valem, e cujo padrão
  // morto continuava desenhado com a cor de um vivo. Agora há UM desenhador: o que
  // muda entre as famílias é a cor, a FORMA do marcador e o nome — nunca o cuidado.
  //
  // `_pontos123` acumula o que foi PINTADO (família, número, preço, forma, cor,
  // fantasma) e sai em `dataset.pat123`: regra de tela que não se mede volta sozinha.
  const _pontos123 = [];
  const _rotulos123 = [];
  _rotulos123.obstaculos = [];
  const _geom = { x, y, padL, plotW };
  const _duasFamilias = familiasNaTela(a).length > 1;

  // OS PONTOS DO PLANO vêm do detector de SWINGS; os do Storm são três candles
  // consecutivos com a MESMA numeração para pontos DIFERENTES. Desenhá-los juntos
  // sem dizer de quem é cada marcador é a colisão que o módulo já declarava.
  // O QUE FOI ANUNCIADO E NÃO COUBE NO ENQUADRAMENTO — ver `resumoEnquadramento`.
  const _fora = [];
  if (pat && camadaVisivel("plano")) {
    const kinds = pat.direction === "venda" ? ["H", "L", "H"] : ["L", "H", "L"];
    const brutos = [["1", pat.p1], ["2", pat.p2], ["3", pat.p3]]
      .map(([lab, p], k) => ({ lab, kind: kinds[k], i: idx[p.date], price: p.price, d: p.date }));
    const pts = brutos.filter((p) => p.i != null);
    const _antes = _pontos123.length;
    desenha123(ctx, _geom, {
      pts, familia: "plano", nome: "Setup123", cor: patColor(pat),
      forma: FORMA_DA_FAMILIA.plano, dash: [4, 3], dist: 14,
      // O gatilho é o convite a operar: um padrão morto não o desenha.
      trigger: pat.trigger, fantasma: ehFantasma(pat), mostraNome: _duasFamilias,
    }, _pontos123, _rotulos123);
    _fora.push(resumoEnquadramento("Setup123", brutos, _pontos123.slice(_antes),
                                   { v0, v1, candles }));
  }

  // OS TRÊS CANDLES DO STORM. Até a DA-088 o Storm só existia no gráfico como linhas
  // de nível — o padrão que dá nome ao método era invisível, enquanto os círculos
  // 1-2-3 na tela eram de OUTRO detector. Agora ele desenha o SEU, com preço e com
  // fantasma, e o marcador em LOSANGO pra não se confundir com o círculo do plano.
  //
  // Com as duas camadas ligadas o marcador do Storm sai MAIS LONGE da vela (os dois
  // padrões podem cair no mesmo candle, e aí os dois selos ocupariam o mesmo pixel).
  const _stormEst = camadaVisivel("storm") && a ? stormEstado(a.storm) : null;
  const _stormPat = _stormEst ? a.storm.pattern : null;
  if (_stormPat) {
    const compra = _stormPat.direction !== "venda";
    const kinds = compra ? ["H", "L", "H"] : ["L", "H", "L"];
    const brutos = [["1", _stormPat.p1], ["2", _stormPat.p2], ["3", _stormPat.p3]]
      .map(([lab, p], k) => ({ lab, kind: kinds[k], i: idx[p.date], price: p.price, d: p.date }));
    const pts = brutos.filter((p) => p.i != null);
    const _antes = _pontos123.length;
    desenha123(ctx, _geom, {
      pts, familia: "storm", nome: "Storm123", cor: stormColor(_stormPat),
      // A LINHA QUE LIGA OS TRÊS PONTOS TAMBÉM PRECISOU DO TRAÇO. Com o marcador
      // seguindo a direção, as duas famílias na MESMA direção saem na mesma cor — e
      // o marcador aguenta (círculo × losango é inconfundível), mas a linha entre os
      // pontos ficava com [3,3] contra o [4,3] do plano: dois tracejados quase
      // idênticos, em cor idêntica, se cruzando. No print de 390px não dava pra
      // seguir qual linha era de quem. O ponto-traço do Storm já é o traço da família
      // nos NÍVEIS (DA-108) — reusá-lo aqui não inventa vocabulário, aplica o que a
      // tela já ensina, e é o mesmo movimento da forma no marcador.
      forma: FORMA_DA_FAMILIA.storm, dash: TRACO_STORM, dist: _duasFamilias ? 34 : 14,
      // o gatilho do Storm já é uma linha de nível rotulada (planZonesStorm) — não
      // se traça o mesmo nível duas vezes
      trigger: null, fantasma: _stormEst === "invalidado", vetado: _stormEst === "vetado",
      estado: stormEstadoTexto(_stormEst, a.storm), mostraNome: _duasFamilias,
    }, _pontos123, _rotulos123);
    _fora.push(resumoEnquadramento("Storm123", brutos, _pontos123.slice(_antes),
                                   { v0, v1, candles }));
  }
  // As marcas de recuo à média entram na MESMA declaração dos padrões — o defeito é o
  // mesmo, e um segundo mecanismo de aviso só daria uma segunda coisa pra esquecer.
  _fora.push(resumoEnquadramento("recuo à média", _marcas, _marcasPint,
                                 { v0, v1, candles }, "marcas"));
  canvas.dataset.pat123 = JSON.stringify(_pontos123);
  // Os rótulos são pintados DEPOIS de todos os marcadores das duas famílias, dentro
  // dos limites do plot — e o dataset guarda o que de fato saiu na tela, não o que se
  // pretendia (é o que deixa "nada foi coberto nem cortado" ser medido).
  const _saiu123 = pintaRotulos123(ctx, _rotulos123,
                                   { topo: padT + 8, base: padT + plotH - 8 });
  canvas.dataset.rotulos123 = JSON.stringify(_saiu123);
  canvas.dataset.rotulos123Geo = JSON.stringify(_saiu123.geo || []);

  // linha fina do preço atual cruzando o gráfico até a régua direita (o número
  // vira pílula no eixo, logo abaixo — nada de caixa sobre as velas).
  if (price != null) {
    const yp = y(price);
    ctx.strokeStyle = "rgba(230,234,242,0.5)"; ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, yp); ctx.lineTo(padL + plotW, yp); ctx.stroke();
    ctx.setLineDash([]);
  }

  // RÓTULO DE CADA NÍVEL desenhado no próprio gráfico ("stop (SL) 128,50"), na
  // altura da linha/faixa: o candle sozinho já diz o que é cada nível, sem obrigar
  // o leitor a cruzar cor com legenda. Empilha com o mesmo de-colisão das pílulas
  // e começa abaixo do carimbo de frame pra não tampá-lo.
  canvas.dataset.levelLabels = "[]";
  if (zones.length) {
    // O rótulo tem de CABER no gráfico. No telefone (~300px de área de plotagem)
    // "recuo à média (MMS50) — não ativa agora 806,67" atravessava a régua do eixo
    // e saía cortado, ainda por cima colidindo com as pílulas de preço (task 020).
    // Escada, do mais informativo ao mínimo: texto inteiro → texto curto → só o
    // preço. O nome nunca se perde de vez — a legenda liga a cor ao nome, e ela
    // agora fica logo abaixo do gráfico no telefone.
    ctx.font = "bold 10px ui-monospace, Menlo, monospace";
    const maxLabelW = plotW - 12;
    const cabe = (txt) => ctx.measureText(txt).width + 14 <= maxLabelW;
    const tagPills = zones.map((z) => {
      const band = z.low != null && z.high != null && z.high > z.low;
      const yl = band ? (y(z.high) + y(z.low)) / 2 : y(z.price);
      const preco = fmtAxis(z.price);
      const inteiro = `${z.tag} ${preco}`;
      const curto = `${z.tagCurto || z.tag} ${preco}`;
      const text = cabe(inteiro) ? inteiro : (cabe(curto) ? curto : preco);
      return { y: yl, ry: yl, text, color: z.color };
    });
    // O chip de R:R agora existe também quando não há número (ele diz o porquê), e
    // o topo dos rótulos tem de descer nos DOIS casos — senão o primeiro rótulo de
    // nível encosta no chip.
    const labelTop = padT + (rrDoGrafico(a).rr ? 52 : 30);
    layoutAxisPills(tagPills, labelTop, padT + plotH - 10, 17);
    // rótulos realmente PINTADOS ficam observáveis (mesmo padrão do zoom em
    // dataset.v0/v1): é assim que o E2E prova que estão no candle, não só na legenda
    canvas.dataset.levelLabels = JSON.stringify(tagPills.map((t) => t.text));
    tagPills.forEach((t) => {
      const w = Math.min(ctx.measureText(t.text).width + 14, maxLabelW);
      if (Math.abs(t.ry - t.y) > 1) {   // deslocado pra não colar: leader até o nível real
        ctx.strokeStyle = t.color; ctx.globalAlpha = 0.45; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(padL + 4, t.ry); ctx.lineTo(padL + 4, t.y); ctx.stroke();
        ctx.globalAlpha = 1;
      }
      roundRect(ctx, padL + 4, t.ry - 8, w, 16, 4);
      ctx.globalAlpha = 0.88; ctx.fillStyle = "#0b0b0b"; ctx.fill(); ctx.globalAlpha = 1;
      ctx.strokeStyle = t.color + "aa"; ctx.lineWidth = 1; ctx.stroke();
      ctx.fillStyle = t.color; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(t.text, padL + 11, t.ry + 0.5);
    });
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

  // AS FAIXAS TAMBÉM SAEM DE VISTA — pelo eixo de PREÇO, não pelo de tempo. Com zoom
  // vertical, uma banda inteira acima do topo (ou abaixo da base) deixa de ser
  // desenhada, e o rótulo dela é ancorado de volta pra dentro do plot por
  // `layoutAxisPills`: sobra um nome flutuando num preço que não está mais na tela.
  const _foraFaixa = zones.filter((z) => {
    const band = z.low != null && z.high != null && z.high > z.low;
    const yTopo = band ? y(z.high) : y(z.price), yBase = band ? y(z.low) : y(z.price);
    return yBase < padT || yTopo > padT + plotH;
  }).map((z) => z.tagCurto || z.tag);

  canvas.dataset.foraDaVista = JSON.stringify(
    { padroes: _fora.filter(Boolean), faixas: _foraFaixa,
      temZoom: !!(canvas._view || canvas._vview) });
  avisoForaDaVista(canvas);
}

// ─────────── O QUE A TELA ANUNCIA E NÃO ESTÁ NO ENQUADRAMENTO (DA-107) ───────────
//
// O canvas não recorta: um ponto do 1-2-3 cujo candle ficou fora da janela de zoom
// continua sendo "desenhado", só que a centenas de pixels da borda esquerda. Do lado
// de fora isso é indistinguível de padrão não detectado — o Samyr olhou a tela, a
// nota abaixo dela prometia "os pontos ficam em cinza como história", e ele perguntou
// "aqui não fez o 1-2-3 do storm?". A promessa não era falsa sobre a COR; era muda
// sobre o LUGAR.
//
// São DUAS causas distintas, e confundi-las dá conselho errado:
//   * fora da JANELA de zoom — o padrão está na série, o dedo é que o empurrou pra
//     fora; tem volta, e a volta é um clique;
//   * sem VELA — a data do ponto não está no período carregado deste tempo gráfico;
//     não há zoom que traga, e prometer um gesto que não resolve é pior que calar.
function resumoEnquadramento(nome, brutos, pintados, janela, termo) {
  const semVela = brutos.filter((p) => p.i == null);
  const foraJanela = pintados.filter((p) => !p.naVista);
  if (!semVela.length && !foraJanela.length) return null;
  const idxs = brutos.filter((p) => p.i != null).map((p) => p.i);
  const antes = idxs.length && Math.max(...idxs) < janela.v0;
  const depois = idxs.length && Math.min(...idxs) >= janela.v1;
  const velas = antes ? janela.v0 - Math.max(...idxs)
              : (depois ? Math.min(...idxs) - janela.v1 + 1 : 0);
  return {
    nome, total: brutos.length, naVista: pintados.length - foraJanela.length,
    semVela: semVela.map((p) => p.d), foraJanela: foraJanela.length,
    lado: antes ? "esquerda" : (depois ? "direita" : null), velas,
    periodo: [janela.candles[0].d, janela.candles[janela.candles.length - 1].d],
    // "pontos" de um 1-2-3, "marcas" de recuo à média: a frase tem de nomear o que o
    // usuário está procurando, senão manda procurar a coisa errada.
    termo: termo || "pontos",
  };
}

// "2026-08-24" / "2026-08-24 14:00" → "24/08" / "24/08 14:00"
function diaMes(d) {
  if (!d || d.length < 10) return d || "";
  const hora = d.length > 10 ? ` ${d.slice(11, 16)}` : "";
  return `${d.slice(8, 10)}/${d.slice(5, 7)}${hora}`;
}

function frasesForaDaVista(dados) {
  const fs = [];
  (dados.padroes || []).forEach((f) => {
    // "os pontos" / "as marcas": artigo casado com o termo, senão a frase que veio
    // desfazer a confusão chega torta.
    const termo = f.termo || "pontos", art = termo === "marcas" ? "as" : "os";
    if (f.semVela.length === f.total) {
      fs.push(`<b>${escapeHtml(f.nome)}</b> não cabe neste tempo gráfico — ${art} ` +
        `${f.total} ${termo} são de ${diaMes(f.semVela[0])} a ` +
        `${diaMes(f.semVela[f.semVela.length - 1])}, e o gráfico carregou de ` +
        `${diaMes(f.periodo[0])} a ${diaMes(f.periodo[1])}.`);
    } else if (f.semVela.length) {
      // "entrou pela metade" descreve um PADRÃO, que é uma figura só; um punhado de
      // marcas independentes não entra pela metade — dele se diz quantas couberam.
      const abre = termo === "marcas" ? ":" : " entrou pela metade —";
      fs.push(`<b>${escapeHtml(f.nome)}</b>${abre} ` +
        `${f.total - f.semVela.length} ${art === "as" ? "das" : "dos"} ${f.total} ` +
        `${termo} estão no período carregado (${diaMes(f.periodo[0])} a ` +
        `${diaMes(f.periodo[1])}).`);
    }
    if (f.foraJanela) {
      const quantos = f.foraJanela === f.total
        ? `${art} ${f.total} ${termo} estão`
        : `${f.foraJanela} ${art === "as" ? "das" : "dos"} ${f.total} ${termo} estão`;
      // A CONTA DE VELAS é o "quanto" que transforma "está fora" em "está ali": sem
      // ela o aviso manda procurar sem dizer onde. Quando o padrão fica a cavaleiro
      // das duas bordas não há um lado só — aí a distância não significa nada e a
      // frase para no fato.
      const onde = f.lado
        ? `${f.velas} vela${f.velas > 1 ? "s" : ""} à ${f.lado} do enquadramento`
        : "fora do enquadramento";
      fs.push(`<b>${escapeHtml(f.nome)}</b>: ${quantos} ${onde}.`);
    }
  });
  if ((dados.faixas || []).length) {
    fs.push(`Fora do enquadramento de preço: ` +
      `${dados.faixas.map(escapeHtml).join(" · ")}.`);
  }
  return fs;
}

// A linha vive COLADA no gráfico e é reescrita a cada redesenho — ela descreve o
// ENQUADRAMENTO, que muda com o dedo, e não a análise, que não muda. Ficar dentro da
// #chartNote (montada uma vez por render) faria o aviso envelhecer no primeiro zoom.
function avisoForaDaVista(canvas) {
  if (!canvas || canvas.id !== "priceChart") return;   // gráficos da comparação não têm a linha
  const el = document.getElementById("chartFora");
  if (!el) return;
  let dados = {};
  try { dados = JSON.parse(canvas.dataset.foraDaVista || "{}"); } catch (_) { dados = {}; }
  const fs = frasesForaDaVista(dados);
  if (!fs.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  // O BOTÃO só aparece quando existe zoom pra desfazer: oferecer "ver a série
  // inteira" quando a série inteira JÁ está na tela manda o usuário num gesto que
  // não muda nada — e um conselho que não resolve gasta a confiança do próximo.
  // O botão NOMEIA o que traz de volta (DA-122). "ver a série inteira" falava só do
  // eixo do TEMPO, e o caso que mais dói é o do PREÇO: o alvo acima do topo
  // visível — o número que sustenta o R:R ficando invisível. A autoescala inclui
  // as faixas do plano (ver `grow(z.price)` em `drawPriceChart`), então o reset é,
  // de fato, o "ajustar à tela" que enquadra velas e níveis juntos.
  const botao = dados.temZoom
    ? `<button type="button" class="cav-btn" id="foraResetBtn">ajustar à tela — velas e níveis do plano</button>`
    : "";
  el.innerHTML = fs.map((f) => `<span class="cn-line">${f}</span>`).join("") + botao;
  el.classList.remove("hidden");
  const btn = document.getElementById("foraResetBtn");
  if (btn) btn.addEventListener("click", () => {
    canvas._view = null; canvas._vview = null;
    drawPriceChart(canvas, canvas._chart, canvas._actionable);
  });
}

function bindChartZoom(canvas) {
  if (!canvas || canvas._zoomBound) return;
  canvas._zoomBound = true;
  // O corpo do gráfico é arrastável desde o primeiro instante (DA-122) — e o
  // cursor diz isso antes de o usuário tentar.
  canvas.style.cursor = "grab";
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
      } else {
        // CORPO DO GRÁFICO = PAN, SEMPRE (DA-122).
        //
        // Era `else if (canvas._view || canvas._vview)`: o arrasto só existia DEPOIS
        // de um zoom. No estado inicial arrastar não fazia nada — e a dica da tela
        // anunciava "arrasta = move 2 eixos" sem dizer que dependia de um passo que
        // ninguém tem como adivinhar. O pedido do Samyr ("deslocar o gráfico
        // arrastando pra baixo pra ver os alvos pra cima") é exatamente o gesto que
        // a dica prometia e o código não entregava.
        //
        // O PAN VERTICAL parte da janela DESENHADA (`_yGeom`), que sem zoom é a
        // autoescala: arrastar passa a criar o `_vview` a partir dela, em vez de
        // exigir que ele já exista. O horizontal continua condicionado ao `_view` —
        // com a série inteira na tela não há janela a deslizar, e fabricar uma
        // faria o gesto "funcionar" andando para o vazio nos dois eixos.
        const g = canvas._yGeom;
        drag = {
          x: e.clientX, y: e.clientY, moveu: false,
          v: canvas._view ? { v0: canvas._view.v0, v1: canvas._view.v1 } : null,
          vv: canvas._vview ? { lo: canvas._vview.lo, hi: canvas._vview.hi }
                            : (g && g.hi > g.lo ? { lo: g.lo, hi: g.hi } : null),
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
      // LIMIAR DE 3px: sem ele, um CLIQUE (que sempre carrega um pixel de tremor)
      // congelaria a autoescala — porque o pan vertical grava `_vview`, e a partir
      // daí a janela de preço deixa de se reajustar sozinha ao dado novo. Um
      // clique não pode ter esse efeito colateral.
      if (!drag.moveu) {
        if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 3) return;
        drag.moveu = true;
      }
      const rect = canvas.getBoundingClientRect();
      // PAN horizontal: desliza a janela de candles (só quando há zoom h ativo)
      if (drag.v) {
        const vis = drag.v.v1 - drag.v.v0;
        const dC = Math.round((e.clientX - drag.x) / (rect.width - PAD_L - PAD_R - PLOT_RIGHT_GAP) * vis);
        const nv0 = Math.max(0, Math.min(drag.v.v0 - dC, N() - vis));
        canvas._view = { v0: nv0, v1: nv0 + vis };
      }
      // PAN vertical: desliza a janela de preço. dy>0 (arrasta pra baixo) sobe a
      // janela → o preço agarrado acompanha o cursor. Sem zoom prévio, `drag.vv`
      // já vem semeado da autoescala (DA-122), então o gesto vale desde o começo.
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
    // O cursor volta a "grab" SEMPRE: o corpo do gráfico é arrastável no estado
    // inicial, e um cursor "default" ali ensinaria de novo que não é (DA-122).
    if (pts.size === 0) { drag = null; vzoom = null; hzoom = null; canvas.style.cursor = "grab"; }
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
  if (_cancelPending && _cancelPending !== runId) clearCancelPending();   // run nova: sem trava herdada (013)
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
    // `refreshing`: a run está no meio do pausar→rebobinar→re-entrar de um
    // "atualizar etapa" (task 002). Ainda é trabalho em curso — não é um término.
    if (snap.status === "running" || snap.refreshing) {
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
    if (snap.status === "running" || snap.refreshing) {
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
  // Barra ÚNICA (task 029): Analisar roda com o método + timeframe escolhidos na barra.
  // Comparar dispara as DUAS (Padrão × Erick, compare=true); 1-2-3 é o atalho estrutural
  // ($0 de LLM); Erick/Padrão vão no method.
  const compare = _barMethod === "compare";
  const method = (_barMethod === "erick" || _METODOS_ESTRUTURAIS.has(_barMethod))
    ? _barMethod : "padrao";
  const timeframe = _barTf || "1d";
  $("runBtn").disabled = true;
  $("resultPanel").classList.add("hidden");
  $("comparePanel").classList.add("hidden");
  $("steps").innerHTML = "";
  resetThinking();   // análise nova: começa com o painel de raciocínio limpo
  try {
    const res = await apiPost("/api/analyze", { ticker, date, method, compare, timeframe });
    const data = await res.json();
    if (res.status === 403 && data.error_code === "need_key") {
      handleNeedKey(data.error);
      return;
    }
    if (!res.ok) throw new Error(data.error || "falha ao iniciar");
    rememberRunToken(data.run_id, data.run_token);
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
let _historyQuery = "";   // busca por ticker ou nome (case-insensitive)

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

// Linha de preço: valor + variação do dia (↑ verde / ↓ vermelho). Sem dado → "—".
function priceLineHtml(p) {
  if (!p || p.price == null) return `<span class="pdash">—</span>`;
  let chg = "";
  if (p.change_pct != null) {
    const up = p.change_pct > 0, dn = p.change_pct < 0;
    const cls = up ? "up" : (dn ? "down" : "flat");
    const arrow = up ? "↑" : (dn ? "↓" : "·");
    chg = ` <span class="pchg ${cls}">${arrow} ${Math.abs(p.change_pct).toFixed(2)}%</span>`;
  }
  return `<span class="pval">${fmtPrice(p.price, p.currency)}</span>${chg}`;
}

function currentHistoryTickers() {
  return [...document.querySelectorAll(".history li[data-ticker]")]
    .map((li) => li.getAttribute("data-ticker"))
    .filter(Boolean);
}

// Só os tickers cujo item está VISÍVEL na viewport (task 011): a lista de observação
// só cresce e pode ter centenas de ativos — o poller de preço não pode puxar todos de
// uma vez. Busca-se o preço só do que está na tela; ao rolar, os novos visíveis entram.
function visibleHistoryTickers() {
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const out = [];
  document.querySelectorAll(".history li[data-ticker]").forEach((li) => {
    const r = li.getBoundingClientRect();
    if (r.bottom >= 0 && r.top <= vh) {   // intersecta a viewport (com folga natural)
      const t = li.getAttribute("data-ticker");
      if (t) out.push(t);
    }
  });
  return out;
}

// Busca o preço live dos tickers visíveis e aplica NOS SPANS, sem repintar a lista
// (evita a "dança"). Reusa o cache do servidor (~45s) — chamadas repetidas são baratas.
async function refreshPrices(tickers) {
  // Sem lista explícita → só os VISÍVEIS (task 011): não martela a fonte com centenas
  // de ativos fora da tela numa watchlist grande.
  const src = tickers && tickers.length ? tickers : visibleHistoryTickers();
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

// Só busca os tickers VISÍVEIS ainda sem preço em cache (novos na tela) — não re-bate a
// fonte a cada repintura de 5s, nem puxa a watchlist inteira (task 011); o poller
// periódico atualiza os visíveis já cacheados.
function refreshNewPrices() {
  const missing = visibleHistoryTickers().filter((t) => !_priceCache.has((t || "").toUpperCase()));
  if (missing.length) refreshPrices(missing);
}

// Ao ROLAR a lista de observação, carrega o preço dos ativos que acabaram de entrar na
// tela (task 011). Debounce curto pra não disparar a cada pixel de scroll.
let _histScrollTimer = null;
function bindHistoryScrollPrices() {
  const box = document.querySelector(".sidebar .history") || document.getElementById("history");
  if (!box || box._priceScrollBound) return;
  box._priceScrollBound = true;
  box.addEventListener("scroll", () => {
    clearTimeout(_histScrollTimer);
    _histScrollTimer = setTimeout(refreshNewPrices, 150);
  }, { passive: true });
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
  // Caixa de busca: filtra a watchlist por ticker ou nome (re-pinta do cache).
  // debounce leve pra não re-pintar a cada tecla em listas grandes; o nome que
  // ainda não resolveu é buscado quando chega (paintHistory re-roda no ensureNames).
  const search = document.getElementById("watchlistSearch");
  if (search && !search._bound) {
    search._bound = true;
    let t;
    search.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        _historyQuery = search.value.trim();
        paintHistory();
      }, 120);
    });
    // Esc limpa a busca; mantém o foco pra digitar de novo.
    search.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && _historyQuery) {
        search.value = "";
        _historyQuery = "";
        paintHistory();
      }
    });
  }
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
    // 1-2-3 (setup123): sem verdict Buy/Hold — o resultado é o estado do setup.
    // Hoje caía em r.status "done" → "CONCLUÍDO" no lugar do veredito. Agora surfamos
    // o setup_state (mesmo campo que a view aberta já mostra no card de setup).
    const isSetup123 = _METODOS_ESTRUTURAIS.has(r.method);
    const setupState = isSetup123 ? (r.setup_state || "sem_dado") : "";
    const v = isSetup123 ? setupState : (r.verdict || r.status || "").toString();
    // title do chip: frase legível do setup (1-2-3) ou o veredito cru nos demais.
    let vTitle = v;
    // contagem de análises do ticker: vem do backend (watchlist varre o index inteiro,
    // task 011); ``n`` (ocorrências na lista) é fallback pra payloads antigos.
    const cnt = r.count || n;
    const badge = cnt > 1 ? `<span class="h-count" title="${cnt} análises">${cnt}</span>` : "";
    // marcador de término em 2º plano (só em run já concluído, some ao abrir)
    const flag = !running && _finishedFlags.get(r.run_id);
    const flagHtml = flag
      ? `<span class="h-flag ${flag}">${flag === "error" ? "erro" : "pronto"}</span>`
      : "";
    let vHtml, vClass, meta;
    if (running) {
      const p = r.progress || {};
      vHtml = `<span class="run-dot"></span>${p.percent || 0}%`;
      vClass = "running";
      meta = `${escapeHtml(p.phase || "processando")} · ${Math.round(r.elapsed || 0)}s`;
    } else {
      if (isSetup123) {
        // Chip 1-2-3: rótulo compacto do setup, COR por estado (a classe vClass
        // abaixo). O title carrega a frase completa de SETUP_PT (acessível, não
        // cabe na coluna).
        const full = SETUP_PT[setupState] || setupState;
        vHtml = escapeHtml(SETUP_COMPACT[setupState] || full);
        vClass = setupState;
        vTitle = full;   // title ganha a frase legível, não o snake_case
      } else {
        vHtml = verdictHtml(v);
        vClass = verdictClass(v).replace("verdict", "").trim();
      }
      // Watchlist densa (task 009/015): a coluna estreita só comporta veredito +
      // DATA à direita sem espremer o nome. Só a DATA (dd/mm, sem hora) — o horário
      // e custo/tempo seguem no cabeçalho da análise aberta.
      const dm = (r.date || "").match(/^\d{4}-(\d{2})-(\d{2})/);
      meta = r.finished_at ? fmtStamp(r.finished_at).split(" ")[0]
                           : (dm ? `${dm[2]}/${dm[1]}` : escapeHtml(r.date || ""));
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
        `<span class="h-sym"><span class="tk-sym">${escapeHtml(t || "?")}</span>${badge}</span>` +
        coHtml +
      `</span>` +
      `<span class="h-right">` +
        `<span class="h-verdict ${vClass}" title="${escapeHtml(running ? "em andamento" : vTitle)}">${vHtml}</span>` +
        `<span class="h-meta">${meta}</span>` +
      `</span>` +
      rm +
      priceHtml +
      // marcador "pronto/erro" na LINHA DO PREÇO (não mais espremido no ticker,
      // que quebrava — task 007): irmão de .h-price (o poller reescreve .h-price,
      // então o flag não pode ser filho dele), alinhado à direita onde há espaço.
      flagHtml +
      `</li>`;
  };
  const filtered = runs.filter((r) => {
    // 1) aba (Todos/Ações/Cripto)
    if (_historyFilter !== "all" && (r.asset_type === "crypto") !== (_historyFilter === "crypto")) return false;
    // 2) busca: ticker ou nome da empresa (case-insensitive). Nome vem do cache
    // async; se ainda não resolveu, filtra só pelo ticker — ao chegar o nome o
    // paintHistory re-roda e a busca passa a casar por nome também.
    if (_historyQuery) {
      const q = _historyQuery.toLowerCase();
      const t = (r.ticker || "").toLowerCase();
      const name = (_nameCache.get((r.ticker || "").toUpperCase()) || "").toLowerCase();
      if (!t.includes(q) && !name.includes(q)) return false;
    }
    return true;
  });
  // um por ticker (o mais recente; a API já devolve do mais novo pro mais velho)
  const seen = new Map();
  filtered.forEach((r) => {
    const k = (r.ticker || "?").toUpperCase();
    if (!seen.has(k)) seen.set(k, { run: r, n: 0 });
    seen.get(k).n += 1;
  });
  ul.innerHTML = seen.size
    ? [...seen.values()].map(({ run, n }) => item(run, n)).join("")
    : `<li class="empty">${_historyQuery ? `Nenhum ativo casando “${escapeHtml(_historyQuery)}”.` : "Nenhuma análise nesta aba."}</li>`;
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
    // credentials same-origin: manda o cookie de sessão do DONO — apagar é owner-gated
    // no servidor (protege o track record público). Público leva 403 e a lista fica.
    const res = await fetch("/api/history/" + encodeURIComponent(t),
                            { method: "DELETE", credentials: "same-origin" });
    if (res.ok) { await loadHistory(); return; }
    if (res.status === 403) alert("Só o dono logado pode remover ativos do histórico.");
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
    updateDateChip();   // chip reflete "Hoje" com a data do servidor (Manaus)
  } catch (e) {
    // fallback: browser-local date if the server is unreachable at boot
    $("date").value = new Date().toLocaleDateString("en-CA");
    updateDateChip();
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

// Ao ESCOLHER uma sugestão do <datalist> (clique OU Enter), o navegador seta o valor
// do campo e dispara um 'input' — que, sem guarda, reagenda a busca e REABRE o popup
// (o Samyr tinha que selecionar de novo). Fechar aqui: cancela o timer, invalida
// qualquer fetch em voo e ESVAZIA o datalist, então o navegador não tem o que reabrir.
function closeTickerSuggest() {
  clearTimeout(_suggestTimer);
  _suggestSeq++;                              // descarta resposta de fetch em voo
  const dl = $("tickerSuggest");
  if (dl) dl.innerHTML = "";                  // sem opções → popup não reabre
}

// Uma seleção de sugestão chega como 'input' com inputType 'insertReplacementText'
// (Chrome/Firefox, tanto no clique quanto no Enter). Alguns navegadores não setam
// inputType — aí o fallback: o valor bate EXATO num símbolo sugerido = seleção.
function isTickerSuggestionPick(ev, raw) {
  if (ev && ev.inputType === "insertReplacementText") return true;
  const dl = $("tickerSuggest");
  const up = (raw || "").trim().toUpperCase();
  if (!dl || !up) return false;
  return Array.from(dl.options).some((o) => (o.value || "").toUpperCase() === up);
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

// Preenche os selects de provedor DE CADA NÍVEL e reflete a config salva nos campos.
// Task 017: não existe mais um provedor único amarrado aos dois modelos — RÁPIDO e
// PESADO são dois pares [provedor + modelo] independentes, sempre visíveis. Os dois
// no mesmo provedor é só um caso particular (o botão "= igual ao Rápido" faz num
// clique). A chave BYOK segue pertencendo ao provedor do PESADO, que é o provedor-base
// da requisição — o rótulo/nota da chave diz isso na cara (renderKeyOwnership).
function renderConfigPanel() {
  if (!_llmMeta || !$("cfgQuickProvider")) return;
  // Provedores owner-only (assinatura do dono, ex.: claude-cli · $0/token) só
  // aparecem pro dono logado — o público nem os vê (o server barra em profundidade).
  const list = (_llmMeta.providers || []).filter((p) => _isOwner || !p.owner_only);
  const fallback = _llmMeta.default_provider || "openai";
  $("cfgKey").value = _llmCfg.apiKey || "";
  $("cfgQuick").value = _llmCfg.quickModel || "";
  $("cfgDeep").value = _llmCfg.deepModel || "";
  renderLevelProviders(list, fallback);
  syncLevelFields();
  // Config salva pode trazer o modelo no formato do provedor ANTERIOR (o bug 016:
  // trocou pra assinatura Claude e ficou "anthropic/claude-opus-5" do OpenRouter →
  // 404). Normaliza ANTES de montar os combos, com os selects de nível já preenchidos.
  // Não-estrito: aqui provedor e modelo foram salvos JUNTOS — corrigir o formato é
  // seguro, trocar um id fora do catálogo (fine-tune próprio) pelo default não é.
  normalizeConfigModels({ strict: false });   // task 016
  migrateSingleProviderConfig();    // task 017
  applyModelCombosForProviders();   // combos refletem o provedor de cada nível (task 014)
  // Mostra o modelo CONCRETO de cada nível em vez de deixar dois campos vazios com
  // placeholder: com provedor por nível na cara (017), "padrão do provedor" escondia
  // justamente o que o usuário veio ver. Só preenche campo VAZIO — nunca sobrescreve.
  preselectDefaults();
  renderOwnerBox();
  renderSubscriptionBox();
  updateConfigBadge();
  renderLaunchModels();   // reflete os modelos salvos nos chips do launcher (task 012)
}

// Popula os selects de provedor de CADA nível (mesma lista visível) e restaura o que
// estava salvo. Config antiga (provedor único, pré-017) migra sozinha: o provedor
// salvo vira o dos DOIS níveis. Provedor salvo que sumiu da lista (owner-only com
// sessão deslogada) cai no default visível — sem quebrar o select.
function renderLevelProviders(list, fallback) {
  const qs = $("cfgQuickProvider");
  const ds = $("cfgDeepProvider");
  if (!qs || !ds) return;
  const opts = (selected) => list.map((p) =>
    `<option value="${escapeHtml(p.id)}"${p.id === selected ? " selected" : ""}>${escapeHtml(p.label)}</option>`
  ).join("");
  const saved = _llmCfg.provider || fallback;    // pré-017: um provedor pros dois
  let q = _llmCfg.quickProvider || saved;
  let d = _llmCfg.deepProvider || saved;
  if (!list.some((p) => p.id === q)) q = list.some((p) => p.id === fallback) ? fallback : (list[0] || {}).id;
  if (!list.some((p) => p.id === d)) d = list.some((p) => p.id === fallback) ? fallback : (list[0] || {}).id;
  qs.innerHTML = opts(q);
  ds.innerHTML = opts(d);
}

// Campos que dependem do provedor DE CADA nível: Base URL (só Ollama/self-host),
// placeholder do modelo (o default daquele provedor) e o rótulo dizendo qual provedor
// manda naquele nível. Cada nível é independente — um pode ser Ollama local (com
// endpoint) e o outro OpenAI (sem), sem um campo pisar no outro.
function syncLevelFields() {
  ["quick", "deep"].forEach((lvl) => {
    const cap = lvl === "deep" ? "Deep" : "Quick";
    const prov = _cfgLevelProvider(lvl);
    const p = _providerMeta(prov);
    const field = $(`cfg${cap}BaseUrlField`);
    if (field) field.classList.toggle("hidden", !(p && p.needs_base_url));
    const model = $(`cfg${cap}`);
    if (model && p) model.placeholder = (lvl === "deep" ? p.default_deep : p.default_quick)
      || "(nome do modelo)";
  });
  syncLevelModelLabels();
  renderKeyOwnership();
}

// Rótulo do campo de modelo: diz em que provedor aquele modelo roda — é o provedor
// daquele nível que define o FORMATO do id (task 016).
function syncLevelModelLabels() {
  [["quick", "cfgQuickLabel", "cfgQuick"],
   ["deep", "cfgDeepLabel", "cfgDeep"]].forEach(([lvl, lid, fid]) => {
    const el = $(lid);
    if (!el) return;
    const prov = _cfgLevelProvider(lvl);
    el.innerHTML = "Modelo " + (prov ? `<span class="orig">(${escapeHtml(prov)})</span>` : "");
    el.setAttribute("for", fid);
  });
}

// De QUEM é a chave: ela vai no header da requisição e o servidor a entrega ao
// provedor-BASE, que é o do PESADO (e ao Rápido quando é o mesmo provedor). Com os
// dois níveis em provedores diferentes que pedem chave, o outro nível precisa da
// chave do servidor/assinatura — e é barrado antes de rodar se não tiver. Dizer isso
// aqui evita o "colei minha chave e mesmo assim deu erro de credencial".
function renderKeyOwnership() {
  const lbl = $("cfgKeyLabel");
  const note = $("cfgKeyNote");
  const qp = _cfgLevelProvider("quick");
  const dp = _cfgLevelProvider("deep");
  if (lbl) {
    lbl.innerHTML = "Chave de API "
      + `<span class="orig">(${escapeHtml(dp || "provedor")}${qp && qp === dp ? " · vale pros dois" : ""})</span>`;
  }
  if (!note) return;
  const needsKey = (id) => { const p = _providerMeta(id); return !!p && !p.key_optional; };
  const split = qp && dp && qp !== dp && needsKey(qp);
  note.classList.toggle("hidden", !split);
  if (split) {
    note.innerHTML = `A chave acima é do provedor do <b>Pesado</b> (${escapeHtml(dp)}). `
      + `O <b>Rápido</b> roda em <b>${escapeHtml(qp)}</b> e usa a credencial do servidor/assinatura — `
      + `sem ela, esse nível é barrado antes de rodar.`;
  }
}

// Config salva antes da 017 tem UM provedor pros dois níveis. Ao abrir o painel ela
// vira o formato novo (o mesmo provedor nos dois) e é persistida — assim os chips do
// launcher, a nota da chave e o corpo da requisição já falam por-nível, sem esperar o
// usuário clicar em Salvar. Não mexe se algum provedor salvo está escondido (owner-only
// com sessão deslogada): ali a tela não representa a escolha do dono.
function migrateSingleProviderConfig() {
  const c = _llmCfg || {};
  if (!c.provider || (c.quickProvider && c.deepProvider)) return false;
  if (_savedProviderHidden()) return false;
  c.quickProvider = c.quickProvider || _cfgLevelProvider("quick") || c.provider;
  c.deepProvider = c.deepProvider || _cfgLevelProvider("deep") || c.provider;
  c.quickBaseUrl = c.quickBaseUrl || c.baseUrl || "";
  c.deepBaseUrl = c.deepBaseUrl || c.baseUrl || "";
  c.advanced = true;
  saveLlmCfg(c);
  return true;
}

// Copia provedor+modelo (e endpoint) do Rápido pro Pesado: o atalho pra rodar TUDO no
// mesmo provedor sem precisar de um "modo simples" separado (task 017).
function mirrorQuickIntoDeep() {
  const qs = $("cfgQuickProvider");
  const ds = $("cfgDeepProvider");
  if (!qs || !ds) return;
  ds.value = qs.value;
  const p = _providerMeta(ds.value);
  // o modelo do PESADO é o do mesmo provedor: usa o do Rápido se ele serve os dois
  // níveis, senão o default pesado do provedor (nunca deixa em formato de outro).
  const qm = $("cfgQuick") ? $("cfgQuick").value.trim() : "";
  if ($("cfgDeep")) $("cfgDeep").value = normalizeModelForProvider(ds.value, qm, "deep")
    || (p && p.default_deep) || "";
  if ($("cfgDeepBaseUrl") && $("cfgQuickBaseUrl")) $("cfgDeepBaseUrl").value = $("cfgQuickBaseUrl").value;
  onLevelProviderChange("deep", { keepModel: true });
}

// Conectar assinatura (task 017; multi-provedor 020): a seção só aparece pro DONO
// logado; o público nem a vê (e o endpoint barra 403 no server, defesa real — o
// esconder é só cosmético). São TRÊS provedores; cada linha COLAPSA ao conectar.
const SUB_PROVIDERS = [
  { key: "openai", label: "ChatGPT", cta: "Conectar com ChatGPT" },
  { key: "anthropic", label: "Claude", cta: "Conectar com Claude" },
  { key: "google", label: "Gemini", cta: "Conectar com Google" },
];
let _subRowsBuilt = false;

function renderSubscriptionBox() {
  const box = $("subscriptionBox");
  if (!box) return;
  if (!_isOwner) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  buildSubscriptionRows();
  refreshSubscriptionStatus();
}

// Monta as 3 linhas (uma vez). Cada linha tem duas caras: "conectar" (botão OAuth no
// estilo do app + fallback avançado de colar token) e "conectada" (colapsada:
// "Label conectada · Desconectar"). O JS alterna as duas conforme o status.
function buildSubscriptionRows() {
  const host = $("subProviders");
  if (!host || _subRowsBuilt) return;
  host.innerHTML = SUB_PROVIDERS.map((p) => `
    <div class="sub-row" data-provider="${p.key}">
      <div class="sub-row-connect">
        <div class="sub-actions">
          <button type="button" class="btn-primary sub-oauth-btn">${escapeHtml(p.cta)} <span aria-hidden="true">↗</span></button>
          <span class="cfg-status sub-status"></span>
        </div>
        <details class="sub-advanced">
          <summary>Avançado — colar o token do ${escapeHtml(p.label)} manualmente</summary>
          <div class="owner-login-row">
            <input type="password" class="sub-token" autocomplete="off" placeholder="token de acesso da assinatura" />
            <button type="button" class="btn-secondary sub-connect-btn">Conectar token</button>
          </div>
        </details>
      </div>
      <div class="sub-row-connected hidden">
        <span class="sub-connected-label"></span>
        <button type="button" class="btn-ghost sub-disc-btn"></button>
      </div>
    </div>`).join("");
  // wiring por linha (o provider vem do data-attribute — nada hardcoded no handler)
  host.querySelectorAll(".sub-row").forEach((row) => {
    const key = row.getAttribute("data-provider");
    row.querySelector(".sub-oauth-btn").addEventListener("click", () => subscriptionOAuthStart(key));
    row.querySelector(".sub-connect-btn").addEventListener("click", () => subscriptionConnect(key));
    row.querySelector(".sub-token").addEventListener("keydown", (e) => { if (e.key === "Enter") subscriptionConnect(key); });
    row.querySelector(".sub-disc-btn").addEventListener("click", () => subscriptionDisconnect(key));
  });
  _subRowsBuilt = true;
}

function _subRow(key) {
  const host = $("subProviders");
  return host ? host.querySelector(`.sub-row[data-provider="${key}"]`) : null;
}

// Reflete o estado colapsado/expandido de cada linha a partir do /status (que já
// funde o registro do app com a DETECÇÃO do login do CLI da box).
async function refreshSubscriptionStatus() {
  try {
    const res = await fetch("/api/subscription/status", { credentials: "same-origin" });
    if (!res.ok) {
      // 403 owner_only enquanto a UI se acha dona = sessão morta no restart: cai pro
      // login em vez de deixar o estado velho na tela (fica coerente com os cliques).
      if (res.status === 403) handleOwnerSessionLost(res, await res.json().catch(() => ({})));
      return;                                              // não-dono/erro: silêncio
    }
    const s = await res.json();
    const providers = s.providers || {};
    SUB_PROVIDERS.forEach((p) => applySubRowState(p, providers[p.key] || {}));
    // Estado de assinatura por provedor (task 014): alimenta a sugestão do claude-cli.
    _subConnected = {};
    Object.keys(providers).forEach((k) => { _subConnected[k] = !!(providers[k] || {}).connected; });
    maybeSuggestClaudeCli();
  } catch (e) { /* rede: mantém como está */ }
}

function applySubRowState(meta, info) {
  const row = _subRow(meta.key);
  if (!row) return;
  const connectView = row.querySelector(".sub-row-connect");
  const connectedView = row.querySelector(".sub-row-connected");
  const label = row.querySelector(".sub-connected-label");
  const discBtn = row.querySelector(".sub-disc-btn");
  if (info.connected) {
    // COLAPSA: esconde botão/texto/Avançado, deixa só a linha compacta.
    const viaServer = info.source === "server";
    label.textContent = viaServer
      ? `${meta.label} conectada · login do servidor`
      : `${meta.label} conectada`;
    // login do servidor não tem registro do app pra remover → oferece "Reconectar"
    // (reabre o OAuth); registro do app → "Desconectar" (remove só o registro).
    discBtn.textContent = viaServer ? "Reconectar" : "Desconectar";
    discBtn.dataset.mode = viaServer ? "reconnect" : "disconnect";
    connectView.classList.add("hidden");
    connectedView.classList.remove("hidden");
  } else {
    connectView.classList.remove("hidden");
    connectedView.classList.add("hidden");
  }
}

// Sessão de dono expirada (o servidor reinicia — deploy da 022 — e as sessões, em
// memória por design [auth.py], zeram): um endpoint só-dono responde 403 owner_only
// enquanto a UI, aberta desde antes do restart, ainda se acha logada. É POR ISSO que
// "Conectar com Google" mostrava "acesso restrito ao dono" mesmo com o dono logado —
// o cookie de sessão morreu no restart, não faltou credentials na chamada. Em vez de
// prender esse enigma numa seção que vai sumir, reflete a verdade: cai pro estado
// deslogado e pede login de novo — aí a ação volta a funcionar (o oauth/start aceita
// o dono com sessão válida). Retorna true se tratou (o chamador não mostra o erro cru).
function handleOwnerSessionLost(res, data) {
  if (!_isOwner) return false;                                  // já deslogado: nada a fazer
  if (!res || res.status !== 403) return false;
  if (!data || data.error_code !== "owner_only") return false;
  _isOwner = false;
  renderConfigPanel();          // esconde a assinatura e mostra o login do dono
  const st = $("ownerStatus");
  if (st) {
    st.textContent = "Sua sessão expirou (o servidor reiniciou). Entre de novo pra conectar assinaturas.";
    st.className = "cfg-status err";
  }
  const pass = $("ownerPass");
  if (pass) pass.focus();
  return true;
}

// Conectar via LINK (task 019, por provedor): pede a URL de autorização ao servidor
// (owner-gated), abre o login oficial do provedor numa nova aba (ação principal =
// ABRIR O LINK, não colar token) e faz poll do status até conectar. O segredo
// (verifier/secret) fica no servidor — o cliente só recebe a URL pública.
let _subPoll = null;
async function subscriptionOAuthStart(provider) {
  const row = _subRow(provider);
  const meta = SUB_PROVIDERS.find((p) => p.key === provider) || { label: provider };
  const btn = row ? row.querySelector(".sub-oauth-btn") : null;
  const st = row ? row.querySelector(".sub-status") : null;
  if (btn) btn.disabled = true;
  if (st) { st.textContent = `abrindo o login do ${meta.label}…`; st.className = "cfg-status sub-status"; }
  try {
    const res = await fetch("/api/subscription/oauth/start", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.authorize_url) {
      if (handleOwnerSessionLost(res, data)) return;   // sessão caiu no restart: já pedimos re-login
      if (st) { st.textContent = (data.error || "não deu pra iniciar"); st.className = "cfg-status sub-status err"; }
      return;
    }
    // Abre o login oficial numa nova aba; o usuário autoriza lá e volta.
    window.open(data.authorize_url, "_blank", "noopener");
    if (st) { st.textContent = `aguardando você autorizar no ${meta.label}…`; st.className = "cfg-status sub-status"; }
    startSubscriptionPoll();
  } catch (e) {
    if (st) { st.textContent = "erro de rede"; st.className = "cfg-status sub-status err"; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Poll do status enquanto o dono autoriza na aba; para ao conectar (qualquer provedor)
// ou após ~5min (teto). O callback grava server-side; aqui só refletimos o estado.
function startSubscriptionPoll() {
  if (_subPoll) clearInterval(_subPoll);
  let tries = 0;
  _subPoll = setInterval(async () => {
    tries++;
    try {
      const res = await fetch("/api/subscription/status", { credentials: "same-origin" });
      if (res.ok) {
        const s = await res.json();
        const anyConnected = Object.values(s.providers || {}).some((p) => p.connected);
        if (anyConnected) { refreshSubscriptionStatus(); }
      }
    } catch (e) { /* rede: tenta de novo */ }
    if (tries > 150) { clearInterval(_subPoll); _subPoll = null; }   // ~5min
  }, 2000);
}

async function subscriptionConnect(provider) {
  const row = _subRow(provider);
  if (!row) return;
  const input = row.querySelector(".sub-token");
  const btn = row.querySelector(".sub-connect-btn");
  const st = row.querySelector(".sub-status");
  const token = (input.value || "").trim();
  if (!token) { st.textContent = "cole o token"; st.className = "cfg-status sub-status err"; return; }
  btn.disabled = true;
  st.textContent = "conectando…"; st.className = "cfg-status sub-status";
  try {
    // O token vai por HEADER (nunca querystring/corpo-logado) e é limpo do input já.
    const res = await fetch("/api/subscription/connect", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Subscription-Token": token },
      body: JSON.stringify({ provider }),
    });
    input.value = "";                       // não retém a credencial no navegador
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.connected) {
      st.textContent = "assinatura conectada"; st.className = "cfg-status sub-status ok";
    } else if (handleOwnerSessionLost(res, data)) {
      return;                                 // sessão caiu no restart: já pedimos re-login
    } else {
      st.textContent = (data.error || "falhou"); st.className = "cfg-status sub-status err";
    }
  } catch (e) {
    st.textContent = "erro de rede"; st.className = "cfg-status sub-status err";
  } finally {
    btn.disabled = false;
    refreshSubscriptionStatus();
  }
}

async function subscriptionDisconnect(provider) {
  const row = _subRow(provider);
  const btn = row ? row.querySelector(".sub-disc-btn") : null;
  // "Reconectar" (login do servidor) reabre o OAuth; "Desconectar" remove o registro
  // do app (NUNCA as creds do CLI da box — isso é garantido server-side).
  if (btn && btn.dataset.mode === "reconnect") { subscriptionOAuthStart(provider); return; }
  try {
    await fetch("/api/subscription/disconnect", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    });
  } catch (e) { /* segue */ }
  refreshSubscriptionStatus();
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

// Linha de status do provedor escolhido num nível (chave opcional / chave de
// fallback no servidor). Os campos em si são sincronizados por syncLevelFields.
function syncProviderStatus(provId) {
  const p = _providerMeta(provId);
  const st = $("cfgStatus");
  if (!st || st.dataset.sticky) return;
  if (p && p.key_optional) st.textContent = "provedor local — chave opcional";
  else if (p && p.server_key) st.textContent = "servidor tem chave de fallback pra este provedor";
  else st.textContent = "";
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
  // Linha "ativo" dentro do painel: com provedor por NÍVEL (017), mostra o que cada
  // nível vai rodar de verdade — um "Ativo: openai" só dizia meia verdade.
  const act = $("cfgActive");
  if (act) {
    const lvl = (level, icon) => {
      const prov = _effLevelProvider(level);
      const model = (level === "deep" ? _llmCfg.deepModel : _llmCfg.quickModel)
        || _providerDefaultModel(level) || "padrão";
      return `${icon} ${prov || "?"} · ${model}`;
    };
    const par = `${lvl("quick", "rápido")}   ${lvl("deep", "pesado")}`;
    if (!_isOwner && !_llmCfg.apiKey) {
      act.textContent = "Sem chave — informe a sua acima ou entre como dono para rodar.";
    } else {
      const fonte = _llmCfg.apiKey ? "sua chave" : "chave do servidor (dono)";
      act.textContent = `Ativo: ${fonte} · ${par}`;
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
      st.textContent = (data.error || "senha incorreta");
      st.className = "cfg-status err";
    }
  } catch (e) {
    st.textContent = "erro de rede"; st.className = "cfg-status err";
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

// Config lida da tela: DOIS pares [provedor + modelo + endpoint] independentes
// (task 017). ``provider``/``baseUrl`` continuam saindo como o do PESADO porque ele é
// o provedor-BASE da requisição (dono da chave BYOK) — o backend e o resto do front
// já falam essa língua, e assim nada de 014/016/027/012 precisou mudar de contrato.
// ``advanced`` vai SEMPRE true: o caminho por-nível virou o único caminho; "os dois
// iguais" é só escolher o mesmo provedor nos dois.
function _readConfigForm() {
  const quickProv = ($("cfgQuickProvider") && $("cfgQuickProvider").value) || "";
  const deepProv = ($("cfgDeepProvider") && $("cfgDeepProvider").value) || "";
  const urlFor = (lvl, prov) => {
    const p = _providerMeta(prov);
    const el = $(lvl === "deep" ? "cfgDeepBaseUrl" : "cfgQuickBaseUrl");
    return (p && p.needs_base_url && el) ? el.value.trim() : "";
  };
  return {
    provider: deepProv,           // provedor-base = o do PESADO (dono da chave)
    apiKey: $("cfgKey").value.trim(),
    // Modelo SEMPRE no FORMATO do provedor do seu nível (task 016): é este objeto que
    // vira _llmCfg no Salvar e alimenta o corpo do analyze/Testar — normalizar aqui
    // impede que um id colado no formato de outro provedor chegue à API. Não-estrito:
    // o que o usuário DIGITOU é escolha dele (fine-tune, deploy próprio); quem reseta
    // sobra de outro provedor é a troca de provedor, não a leitura do formulário.
    quickModel: normalizeModelForProvider(quickProv, $("cfgQuick").value.trim(), "quick", { strict: false }),
    deepModel: normalizeModelForProvider(deepProv, $("cfgDeep").value.trim(), "deep", { strict: false }),
    quickProvider: quickProv,
    deepProvider: deepProv,
    quickBaseUrl: urlFor("quick", quickProv),
    deepBaseUrl: urlFor("deep", deepProv),
    baseUrl: urlFor("deep", deepProv),   // endpoint-base = o do PESADO
    advanced: true,
  };
}

function setCfgStatus(msg, kind) {
  const st = $("cfgStatus");
  if (!st) return;
  st.textContent = msg || "";
  st.className = "cfg-status" + (kind ? " " + kind : "");
  st.dataset.sticky = msg ? "1" : "";
}

// Assinatura conectada por provedor (task 014): povoado por refreshSubscriptionStatus.
// _subConnected.anthropic = a assinatura Claude está conectada (via CLI/OAuth).
let _subConnected = {};

// Troca do provedor de UM nível (task 014 → primária na 017): o modelo daquele nível
// vai pro FORMATO do novo provedor e o combo lista os modelos DELE (ao vivo se der,
// senão catálogo). Nunca deixa o nível com um modelo de outro provedor. Um id
// compatível (mesma família, ex.: claude-sonnet-5 do Anthropic pago → assinatura) é
// PRESERVADO; só o incompatível cai no default (task 016). ``keepModel`` é pra quem
// já pôs o modelo certo antes de chamar (o "= igual ao Rápido").
function onLevelProviderChange(level, opts) {
  const sel = level === "deep" ? $("cfgDeepProvider") : $("cfgQuickProvider");
  const fid = level === "deep" ? "cfgDeep" : "cfgQuick";
  const dk = level === "deep" ? "default_deep" : "default_quick";
  const prov = sel ? sel.value : "";
  const p = _providerMeta(prov);
  setCfgStatus("");
  if ($(fid) && !(opts && opts.keepModel)) {
    const cur = $(fid).value.trim();
    $(fid).value = cur ? normalizeModelForProvider(prov, cur, level) : ((p && p[dk]) || "");
  }
  syncLevelFields();
  syncProviderStatus(prov);
  applyModelCombosForProviders();
  const form = _readConfigForm();
  // Dono com assinatura conectada escolhendo o Anthropic PAGO: a dica vale mais que o
  // "N modelos" — as duas ocupam a mesma linha de status, então só uma escreve.
  const suggest = _isOwner && !!_subConnected.anthropic && prov === "anthropic";
  // a chave BYOK é do provedor-base (o do PESADO); um nível em outro provedor lista
  // pela env do dono (ou cai no catálogo curado).
  refreshModelsForProvider(prov, {
    apiKey: prov === form.provider ? form.apiKey : "",
    baseUrl: level === "deep" ? form.deepBaseUrl : form.quickBaseUrl,
    status: !suggest,
  });
  if (suggest) maybeSuggestClaudeCli();
  renderLaunchModels();
}

// Dono com assinatura Claude conectada mas usando o Anthropic PAGO (simples ou por-nível):
// sugere a assinatura ($0/token) — a escolha óbvia pra Claude sem gastar chave (task 014).
function maybeSuggestClaudeCli() {
  if (!_isOwner || !_subConnected.anthropic) return;
  if (_cfgLevelProvider("quick") === "anthropic" || _cfgLevelProvider("deep") === "anthropic") {
    setCfgStatus("Assinatura Claude conectada — escolha “Claude — assinatura ($0/token)” em vez do Anthropic pago pra rodar sem gastar chave.", "");
  }
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
  // Provedor POR NÍVEL (task 017 — layout primário): cada troca ressincroniza o modelo
  // daquele nível pros modelos do SEU provedor (task 014) — nunca deixa Anthropic com
  // modelo OpenAI. O catálogo reflete na hora; a lista ao vivo enriquece depois.
  const qp = $("cfgQuickProvider"); if (qp) qp.addEventListener("change", () => onLevelProviderChange("quick"));
  const dp = $("cfgDeepProvider"); if (dp) dp.addEventListener("change", () => onLevelProviderChange("deep"));
  // Conveniência: rodar tudo no mesmo provedor sem um "modo simples" separado.
  const mirror = $("cfgSameAsQuick");
  if (mirror) mirror.addEventListener("click", mirrorQuickIntoDeep);
  // Endpoint por nível (Ollama/self-host): trocar o endereço recarrega a lista daquele
  // nível — é a URL que decide quais modelos existem.
  [["quick", "cfgQuickBaseUrl"], ["deep", "cfgDeepBaseUrl"]].forEach(([lvl, id]) => {
    const el = $(id);
    if (el) el.addEventListener("change", () => onLevelProviderChange(lvl, { keepModel: true }));
  });
  // Ao DIGITAR/COLAR a chave: testa e puxa os modelos automaticamente (debounce).
  $("cfgKey").addEventListener("input", scheduleModels);
  $("cfgKey").addEventListener("paste", () => setTimeout(refreshModels, 0));
  $("cfgSave").addEventListener("click", () => {
    saveLlmCfg(_readConfigForm());
    setCfgStatus("salvo ✓", "ok");
    updateConfigBadge();
    renderLaunchModels();   // config salvo → chips do launcher refletem na hora (task 012)
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
  // Conectar assinatura: só-dono (a seção só aparece logado). As linhas por provedor
  // (task 020) são montadas e "wired" em buildSubscriptionRows(); caminho principal =
  // botão OAuth (task 019), colar token vira fallback avançado (task 017 preservada).
}

// Erro de "precisa de chave" (403 need_key): abre a config e aponta o caminho.
function handleNeedKey(msg) {
  $("formError").textContent = msg || "Informe sua chave nas Configurações.";
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
// Cache de modelos POR PROVEDOR (task 014): listas ao vivo (BYOK) vão pra cá por
// provedor; assim trocar de provedor (simples OU por-nível) reflete os modelos DAQUELE
// provedor sem mismatch. Sem lista ao vivo, cai no catálogo curado da meta (instantâneo).
const _liveModels = {};           // { [providerId]: [{id,name,price_*}] }

// Itens do catálogo curado do provedor (vem da meta /api/config, task 014) — {id,name}.
// Degrada pra [] se a meta ainda não tem `models` (server antigo pré-restart).
function _providerCatalogItems(prov) {
  const p = _providerMeta(prov);
  const list = (p && Array.isArray(p.models)) ? p.models : [];
  return list.map((m) => ({ id: m.id, name: m.name || m.id, price_in: null, price_out: null }));
}

// Modelos EFETIVOS de um provedor pro dropdown: lista ao vivo (se já buscada) senão o
// catálogo curado. Nunca devolve o modelo de OUTRO provedor — mata o mismatch (014).
function _itemsForProvider(prov) {
  return (_liveModels[prov] && _liveModels[prov].length) ? _liveModels[prov]
    : _providerCatalogItems(prov);
}

// ---- Formato do id do modelo POR PROVEDOR (task 016) ------------------------
// O id NÃO é portável: OpenRouter usa "vendor/modelo" (anthropic/claude-opus-5); a API
// Anthropic — e a assinatura claude-cli, que fala a mesma API — só entende o id PURO.
// Trocar de provedor deixando o id do outro formato dava 404 no meio da run. As regras
// vêm do BACKEND na meta (`id_format`), então front e servidor normalizam igual —
// aqui é a correção na tela, lá é a rede de proteção final.
function _idFormat(prov) {
  const p = _providerMeta(prov);
  return (p && p.id_format) || { style: "free", families: [], vendor_ns: null };
}

// Grafia frouxa: o OpenRouter escreve a versão com PONTO (claude-haiku-4.5) onde a
// API nativa usa TRAÇO (claude-haiku-4-5) — casar assim acha o equivalente certo.
function _looseId(id) {
  return String(id || "").trim().toLowerCase().replace(/\./g, "-");
}

function _matchesFamily(families, id) {
  if (!families || !families.length) return true;   // sem família: aceita qualquer id
  const low = String(id || "").toLowerCase();
  return families.some((f) => low.startsWith(f));
}

// Namespace "vendor/" de um id puro, pela família (rota inversa, pro OpenRouter).
function _vendorNsForId(id) {
  const list = (_llmMeta && _llmMeta.providers) || [];
  for (const p of list) {
    const f = p.id_format || {};
    if (f.vendor_ns && (f.families || []).length && _matchesFamily(f.families, id)) return f.vendor_ns;
  }
  return null;
}

function _providerDefaultFor(prov, level) {
  const p = _providerMeta(prov);
  if (!p) return "";
  return (level === "deep" ? p.default_deep : p.default_quick) || "";
}

// Id do modelo NO FORMATO do provedor. Tira o "vendor/" sobrando e casa a grafia no
// catálogo curado (o OpenRouter escreve 4.5, a API nativa 4-5).
// `opts.strict` decide o resto: na TROCA de provedor (true) um id de outra FAMÍLIA é
// resto do provedor anterior e cai no default; ao LER o que o usuário digitou (false)
// só o formato é corrigido — um fine-tune/deploy fora do catálogo é escolha legítima.
// Id já puro e da família certa passa intacto nos dois casos: o catálogo envelhece e
// resetar um modelo novo e válido seria pior que o bug.
function normalizeModelForProvider(prov, model, level, opts) {
  const strict = !(opts && opts.strict === false);
  const raw = String(model || "").trim();
  if (!raw || !prov) return raw;
  const fmt = _idFormat(prov);
  if (fmt.style === "free") return raw;
  if (fmt.style === "vendor_slash") {
    if (raw.indexOf("/") >= 0) return raw;
    const ns = _vendorNsForId(raw);
    return ns ? ns + "/" + raw : raw;
  }
  const def = _providerDefaultFor(prov, level);
  const bare = raw.split("/").pop();
  if (!bare) return def || raw;
  if (bare !== raw) {
    // veio com namespace ⇒ id de OUTRO formato: casa no catálogo, senão tira a barra
    // (e, no estrito, cai no default — namespace de outro provedor é 404 na certa).
    const hit = _providerCatalogItems(prov).find((m) => _looseId(m.id) === _looseId(bare));
    if (hit) return hit.id;
    return strict ? (def || bare) : bare;
  }
  if (_matchesFamily(fmt.families, bare) || !strict) return bare;
  return def || bare;
}

// Algum provedor SALVO sumiu da lista? (provedor owner-only com a sessão deslogada: o
// select cai no default visível). Aí a tela NÃO representa a escolha do usuário e
// normalizar contra o provedor visível apagaria os modelos dele — melhor não tocar.
function _savedProviderHidden() {
  const c = _llmCfg || {};
  const sel = $("cfgQuickProvider");
  if (!sel) return false;
  const visible = Array.from(sel.options).map((o) => o.value);
  return [c.quickProvider, c.deepProvider, c.provider]
    .some((id) => id && !visible.includes(id));
}

// Põe os DOIS campos de modelo no formato do provedor do SEU nível. Devolve true se
// algo mudou — o caso do bug: config salva com id do provedor anterior (OpenRouter)
// sobrevivendo à troca pro claude-cli. Quando muda, persiste já normalizado, senão o
// chip do launcher e a próxima run voltavam a mandar o id velho.
function normalizeConfigModels(opts) {
  if (_savedProviderHidden()) return false;
  let changed = false;
  [["quick", "cfgQuick"], ["deep", "cfgDeep"]].forEach(([lvl, fid]) => {
    const el = $(fid);
    if (!el) return;
    const norm = normalizeModelForProvider(_cfgLevelProvider(lvl), el.value, lvl, opts);
    if (norm !== el.value) { el.value = norm; changed = true; }
  });
  if (changed) {
    _llmCfg = _llmCfg || {};
    if ($("cfgQuick")) _llmCfg.quickModel = $("cfgQuick").value;
    if ($("cfgDeep")) _llmCfg.deepModel = $("cfgDeep").value;
    saveLlmCfg(_llmCfg);
    renderLaunchModels();
  }
  return changed;
}

// Provedor de um nível NO CONFIG: lê o select daquele nível — cada um tem o seu
// (task 017), não há mais provedor único por trás.
function _cfgLevelProvider(level) {
  const sel = level === "deep" ? $("cfgDeepProvider") : $("cfgQuickProvider");
  return (sel && sel.value) || "";
}

// Realimenta cada combo do config com os modelos do SEU provedor de nível (task 014):
// o Rápido lista o provedor do Rápido e o Pesado o do Pesado. Com os dois no mesmo
// provedor as duas listas coincidem — é o mesmo caminho, sem caso especial.
function applyModelCombosForProviders() {
  if (_modelCombos.cfgQuick) _modelCombos.cfgQuick.setItems(_itemsForProvider(_cfgLevelProvider("quick")));
  if (_modelCombos.cfgDeep) _modelCombos.cfgDeep.setItems(_itemsForProvider(_cfgLevelProvider("deep")));
}

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

// Normaliza a resposta do endpoint em {id,name,price_*}; aceita ids soltos (compat).
function normalizeModelItems(models) {
  return (models || [])
    .map((m) => (typeof m === "string"
      ? { id: m, name: m, price_in: null, price_out: null } : m))
    .filter((m) => m && m.id);
}

// Alimenta os comboboxes com uma lista ao vivo, cacheando-a POR PROVEDOR (task 014).
// Sem provedor (limpar) só zera a lista corrente; os combos voltam ao catálogo do
// provedor de cada nível via applyModelCombosForProviders.
function fillModelLists(models, provider) {
  const items = normalizeModelItems(models);
  _modelItems = items;                        // compat (última lista carregada)
  if (provider) _liveModels[provider] = items;
  applyModelCombosForProviders();             // cada combo reidrata pelo SEU provedor
}

// Pré-seleciona o default de CADA nível pelo SEU provedor (task 014): no avançado o
// Rápido usa o default do provedor do Rápido e o Pesado o do Pesado. Só preenche
// campo vazio cujo default existe na lista do provedor (senão deixa escolher/digitar).
function preselectDefaults() {
  [["quick", "cfgQuick", "default_quick"], ["deep", "cfgDeep", "default_deep"]].forEach(
    ([lvl, fid, dk]) => {
      const prov = _cfgLevelProvider(lvl);
      const p = _providerMeta(prov);
      if (!p || !$(fid)) return;
      const ids = new Set(_itemsForProvider(prov).map((m) => m.id));
      if (!$(fid).value && p[dk] && ids.has(p[dk])) $(fid).value = p[dk];
    });
}

// Provider dá pra listar AO VIVO agora? claude-cli (assinatura) vem do catálogo, sem
// /models; os demais seguem a regra de chave/owner. (task 014)
function _canListProvider(provider, apiKey, baseUrl) {
  if (provider === "claude-cli") return false;     // assinatura: catálogo curado
  if (_isOwner) return true;                       // dono usa a env do servidor
  if (provider === "openrouter") return true;      // catálogo público
  if (provider === "ollama") return !!baseUrl;
  return (apiKey || "").length >= 8;               // demais: precisa da chave
}

let _modelsTimer = null;
let _modelsAbort = null;
let _modelsSeq = 0;

// Testa a chave E puxa a lista de modelos de UM provedor (POST /api/models), cacheando
// por provedor. Cancela a requisição anterior. Sucesso popula; provedor sem listagem
// (claude-cli) ou falha → cai no CATÁLOGO curado (nunca modelo de outro provedor).
async function refreshModelsForProvider(provider, { apiKey = "", baseUrl = "", status = false } = {}) {
  if (!_canListProvider(provider, apiKey, baseUrl)) {
    applyModelCombosForProviders();               // catálogo do provedor
    if (status) setCfgStatus(provider === "claude-cli"
      ? "assinatura Claude — modelos $0/token (catálogo)" : "", provider === "claude-cli" ? "ok" : "");
    return;
  }
  const seq = ++_modelsSeq;
  if (_modelsAbort) _modelsAbort.abort();
  _modelsAbort = new AbortController();
  if (status) setCfgStatus("testando chave e carregando modelos…", "");
  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers["X-LLM-Key"] = apiKey;
  const body = { llm_provider: provider };
  if (baseUrl) body.backend_url = baseUrl;
  try {
    const res = await fetch("/api/models", {
      method: "POST", headers, credentials: "same-origin",
      body: JSON.stringify(body), signal: _modelsAbort.signal,
    });
    const data = await res.json();
    if (seq !== _modelsSeq) return;   // resposta velha: a chave/provedor já mudou
    if (data.ok) {
      fillModelLists(data.models, provider);
      preselectDefaults();
      if (status) setCfgStatus(`chave válida — ${data.count} modelos carregados`, "ok");
    } else {
      applyModelCombosForProviders();             // catálogo do provedor
      if (status) setCfgStatus(`${data.error || "não deu pra listar os modelos"}`, "err");
    }
  } catch (e) {
    if (e.name === "AbortError" || seq !== _modelsSeq) return;
    if (status) setCfgStatus("erro de rede ao listar modelos", "err");
  }
}

// Entry-point do provedor SIMPLES/base (mesma assinatura de antes — mantém os callers).
// Testa a chave e puxa os modelos dos provedores EM USO — os dois níveis (task 017).
// Provedores iguais nos dois = uma requisição só. O status (ok/erro) é o do provedor-base
// (o do PESADO), que é o dono da chave digitada.
async function refreshModels() {
  const form = _readConfigForm();
  // SEQUENCIAL de propósito: o controle de cancelamento (_modelsAbort/_modelsSeq) é
  // um só, pra a digitação da chave descartar respostas velhas — dois pedidos em
  // paralelo se cancelariam.
  await refreshModelsForProvider(form.deepProvider, {
    apiKey: form.apiKey, baseUrl: form.deepBaseUrl, status: true,
  });
  if (form.quickProvider && form.quickProvider !== form.deepProvider) {
    await refreshModelsForProvider(form.quickProvider, {
      apiKey: "", baseUrl: form.quickBaseUrl,
    });
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
// (POST /api/test-model) e mostra a latência de cada (ou a mensagem de erro), SEM
// rodar a análise. A chave viaja só no header X-LLM-Key; nada dela aparece na tela.
let _modelTestAbort = null;
async function testModel() {
  const form = _readConfigForm();
  // Mesmo gate do analyze: sem login do dono e sem chave própria não há o que testar.
  if (!_isOwner && !(form.apiKey || "").length) {
    renderModelTest({ error: "Informe sua chave nas Configurações antes de testar o modelo." });
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
  // Cross-provider por nível (task 027/014): sem isso o "Testar modelo" testava tudo no
  // provedor-base (ex.: Rápido=claude-cli mas pingava OpenAI → falso erro de crédito).
  if (form.advanced) {
    body.advanced = true;
    if (form.quickProvider) body.quick_provider = form.quickProvider;
    if (form.deepProvider) body.deep_provider = form.deepProvider;
  }
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

// Desenha o resultado do teste: uma linha por modelo (rápido / pesado) com
// a latência + trecho, ou a mensagem humana. `sample`/`error` vêm do modelo →
// sempre escapados (anti-XSS). Erro de topo (need_key/rede) vira uma linha só.
function renderModelTest(data) {
  const box = $("cfgModelTest");
  if (!box) return;
  box.classList.remove("hidden");
  const models = (data && data.models) || [];
  if ((!models.length) && data && data.error) {
    box.innerHTML = `<div class="mt-row err">${escapeHtml(String(data.error))}</div>`;
    return;
  }
  if (!models.length) {
    box.innerHTML = '<div class="mt-row err">não deu pra testar o modelo</div>';
    return;
  }
  box.innerHTML = models.map((m) => {
    // O pictograma que abria a linha saiu (DA-076). O nível continua dito: `label` já é
    // "Rápido"/"Pesado" por extenso — repetir a palavra seria "rápido rápido".
    const label = escapeHtml(String(m.label || m.role || ""));
    const name = escapeHtml(String(m.model || "(padrão do provedor)"));
    if (m.ok) {
      const sample = m.sample ? ` — “${escapeHtml(String(m.sample))}”` : "";
      return `<div class="mt-row ok">${label} <code>${name}</code>: `
        + `<b>${escapeHtml(fmtLatency(m.latency_ms))}</b>${sample}</div>`;
    }
    return `<div class="mt-row err">${label} <code>${name}</code>: `
      + `${escapeHtml(String(m.error || "falhou"))}</div>`;
  }).join("");
}

// ---- SCAN DE PORTFÓLIO (28/08): gatilhos 1-2-3 a $0 de LLM ---------------------
// O olho barato: varre a watchlist em 1d+4h+1h, classifica pela distância do preço ao
// gatilho e oferece a análise completa (Padrão/Erick) a um clique no que estiver
// EM GATILHO. Estados (vocabulário único do backend scanner.py):
let _scanData = null;        // último scan completo (pra re-pintar ao trocar filtro)
let _scanEstadoFilter = null; // estado selecionado no filtro de chips (null = todos)
let _scanAt = null;          // QUANDO o scan que está na tela foi tirado (task 014)
// Os rótulos do scan passam a ser as palavras do EIXO (DA-121): "EM MOVIMENTO" e
// "FORMANDO" descreviam o MECANISMO e cada um convidava a uma leitura temporal
// própria. Agora a fase é o que se lê, e o mecanismo desceu para o `title` — que é
// onde ele sempre coube melhor que numa célula estreita.
//
// `em_gatilho` é a exceção declarada: ali o rótulo é COMPRA/VENDA porque a DIREÇÃO
// é a informação que decide, e a fase ("na entrada") já está dita pela seção de
// Sinais e pela cor. Trocá-la por "NA ENTRADA" apagaria de qual lado se entra.
const SCAN_ESTADO_PT = {
  em_gatilho: { compra: ["COMPRA"], venda: ["VENDA"] },
  em_movimento: ["JÁ ANDOU"],
  invalidou: ["INVALIDADO"],
  formando: ["AGUARDANDO"],
  sem_setup: ["sem setup", "·"],
  sem_dado: ["sem dado"],
};
function scanEstadoChip(estado, direction, andado) {
  const entry = SCAN_ESTADO_PT[estado] || [estado];
  // em_gatilho é direção-aware: COMPRA (verde) ou VENDA (vermelho) — a ação na cara.
  if (estado === "em_gatilho") {
    const dir = direction === "venda" ? "venda" : "compra";
    const [pt] = entry[dir];
    // O chip diz COMPRA/VENDA, e no scan isso é SEMPRE o 1-2-3 (o estado vem do
    // pattern). Diz isso no title pra que a mesma palavra na tela de análise —
    // onde também existe o recuo à média — não vire dúvida sobre qual setup.
    const tit = `padrão 1-2-3 de ${dir} acionado (gatilho rompido)`;
    return `<span class="scan-chip ${dir}" title="${escapeHtml(tit)}">${escapeHtml(pt)}</span>`;
  }
  const [pt] = entry;
  const cls = estado === "em_movimento" ? "scan-chip movimento"
    : estado === "invalidou" ? "scan-chip invalidou"
    : "scan-chip";
  // O MECANISMO desceu pro title (DA-121): a célula mostra a FASE, e o que
  // exatamente se espera (ou o que ficou para trás) fica a um hover — sem que a
  // distinção se perca, que é a invariante da DA-078.
  const mec = mecanismoPt(estado);
  // "em movimento" sozinho não distingue um rompimento de ontem de um trade que já
  // andou 91% do caminho — e é essa diferença que decide se ainda dá pra entrar.
  // O percurso entra NO CHIP, que é o que se lê de relance na lista.
  const pct = andado == null ? "" :
    ` <span class="scan-andado">${Math.round(andado)}%</span>`;
  const faseTxt = [faseAjuda(faseDoScanEstado(estado)), mec].filter(Boolean).join(" — ");
  const andadoTxt = andado == null ? "" :
    `o preço já andou ${Math.round(andado)}% do caminho do gatilho até o alvo — ` +
    `sobra ${Math.round(100 - andado)}%`;
  const tit = [faseTxt, andadoTxt].filter(Boolean).join(". ");
  const titAttr = tit ? ` title="${escapeHtml(tit)}"` : "";
  return `<span class="${cls}"${titAttr}>${escapeHtml(pt)}${pct}</span>`;
}
function scanFmt(n) { return n == null ? "—" : Number(n).toLocaleString("pt-BR", { maximumFractionDigits: 2 }); }

async function openScanPanel() {
  $("scanPanel").classList.remove("hidden");
  $("resultPanel").classList.add("hidden");
  $("comparePanel").classList.add("hidden");
  $("progressPanel").classList.add("hidden");
  clearActiveRun();
  // A watchlist e o último scan salvo são leituras baratas e independentes; a
  // varredura é a cara. O salvo tem de PINTAR antes de a varredura começar —
  // é ele que faz `runScan` enxergar um "anterior" a preservar em vez de abrir
  // com a tela vazia (ver :func:`loadScanSalvo`).
  const wl = loadScanWatchlist();
  await loadScanSalvo();
  await Promise.all([wl, runScan()]);
}

// O painel nasce com a ÚLTIMA VARREDURA CONHECIDA, do disco do servidor.
//
// A task 014 fez o resultado anterior sobreviver a uma re-varredura, mas só
// DENTRO da sessão do navegador: ao abrir a página não existe anterior nenhum, e
// a tela ficava vazia os 8–20s da varredura (medido: 20,0s depois de o Storm
// entrar). O servidor não tinha onde guardar isso — o memo dele dura 5s e morre
// no restart, e o scans.jsonl só registra os em_gatilho. Agora tem
// (``/api/scan/salvo``), e a abertura mostra informação em vez de espera.
//
// Fail-open e silencioso: sem nada salvo (primeira vez de todas), endpoint fora
// do ar ou resposta ilegível, a função não pinta nada e `runScan` cai no
// comportamento de primeira carga — nunca uma lista vazia inventada, que se leria
// como "não há nada em gatilho".
async function loadScanSalvo() {
  if (_scanData) return;              // já há scan na tela: não regride pro salvo
  try {
    const res = await fetch("/api/scan/salvo");
    if (!res.ok) return;
    const data = await res.json();
    if (!data || !(data.ativos || []).length) return;
    // Sem carimbo do servidor não dá pra dizer DE QUANDO isto é, e o fallback
    // (relógio local) dataria de AGORA um scan que pode ter dias — exatamente o
    // disfarce que esta entrega existe pra impedir. Sem data honesta, não entra.
    if (!data.gerado_em) return;
    paintScan(data);
  } catch (e) { /* sem salvo: a varredura pinta quando chegar */ }
}

async function loadScanWatchlist() {
  try {
    const res = await fetch("/api/watchlist");
    const data = await res.json();
    const box = $("scanWatchlist");
    const owner = _isOwner;
    const chips = (data.tickers || []).map((w) => {
      const t = w.ticker || "";
      const rm = owner ? ` <button type="button" class="wl-x" data-wl-x="${escapeHtml(t)}" title="Remover da watchlist">✕</button>` : "";
      return `<span class="wl-chip" data-wl="${escapeHtml(t)}">${escapeHtml(t)}${rm}</span>`;
    }).join("");
    const add = owner
      ? `<form id="wlAddForm" class="wl-add"><input id="wlAddInput" placeholder="adicionar ativo (ex.: NVDA, BTC-USD)" autocomplete="off" /><button type="submit">＋</button></form>`
      : `<span class="hint">login do dono pra editar a lista</span>`;
    box.innerHTML = (chips || '<span class="hint">watchlist vazia — adicione ativos</span>') + add;
    box.querySelectorAll("[data-wl-x]").forEach((b) => b.addEventListener("click", () => watchlistEdit("remove", b.dataset.wlX)));
    const f = box.querySelector("#wlAddForm");
    if (f) f.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const v = $("wlAddInput").value.trim();
      if (v) watchlistEdit("add", v);
    });
    box.querySelectorAll("[data-wl]").forEach((c) => c.addEventListener("click", (e) => {
      if (e.target.classList.contains("wl-x")) return;
      $("ticker").value = c.dataset.wl;   // preenche o launcher; o usuário decide o método
    }));
  } catch (e) { $("scanWatchlist").innerHTML = `<span class="hint">watchlist indisponível (${escapeHtml(e.message)})</span>`; }
}

async function watchlistEdit(action, ticker) {
  try {
    const res = await apiPost("/api/watchlist", { action, ticker });
    if (res.status === 403) { $("scanHint").textContent = "só o dono edita a watchlist (faça login)"; return; }
    if (!res.ok) throw new Error((await res.json()).error || "falha");
    await loadScanWatchlist();
    $("scanHint").textContent = "";
  } catch (e) { $("scanHint").textContent = e.message; }
}

// A linha de aviso ao lado do resumo: "atualizando…" durante a varredura e
// "falhou, isto aqui é de tal hora" quando ela não chega. Vazio esconde.
function scanNotice(html, erro) {
  const el = $("scanNotice");
  if (!el) return;
  el.innerHTML = html || "";
  el.classList.toggle("hidden", !html);
  el.classList.toggle("err", !!erro);
}

// QUANDO o scan que está na tela foi tirado. O servidor carimba a varredura
// (``gerado_em``, Manaus, offset-aware) — sem esse campo a tela só saberia a hora
// em que o JSON CHEGOU nela, e um resultado vindo do disco (aberto o painel) ou do
// memo do servidor se passaria por recém-saído. O fallback pro relógio local (o
// que a task 014 fazia) só alcança a varredura que ACABOU de chegar, e aí ele é
// verdade; o resultado lido do disco sem carimbo é recusado antes de chegar aqui
// (:func:`loadScanSalvo`), porque nele o relógio local seria mentira.
function scanQuando(data) {
  const iso = data && data.gerado_em;
  if (iso) {
    const d = new Date(iso);
    if (!isNaN(d.getTime())) return d;
  }
  return new Date();
}

// UM instante vira texto legível: "14:32", "ontem 12:00", "29/08 14:32".
//
// **Dado de outro dia não se disfarça de recente**: só a hora num carimbo de ontem
// é indistinguível de um de agora, e é justamente o que veio do disco (que
// sobrevive ao restart e pode ter dias) que essa leitura decidiria errado. A
// partir de ontem o DIA entra na frente da hora e o carimbo se declara velho.
function carimboDeInstante(d) {
  if (!d || isNaN(d.getTime())) return null;
  const hora = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const meiaNoite = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const dias = Math.round((meiaNoite(new Date()) - meiaNoite(d)) / 86400000);
  if (dias <= 0) return { txt: hora, velho: false };
  if (dias === 1) return { txt: `ontem ${hora}`, velho: true };
  const dm = d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
  return { txt: `${dm} ${hora}`, velho: true };
}

// O carimbo do conjunto que está na tela — a passada MAIS RECENTE. Devolve ``null``
// quando não há scan nenhum: nunca uma string vazia, que se leria como "sem hora".
function scanCarimbo() {
  return carimboDeInstante(_scanAt);
}

// O carimbo de UM ativo — só para quem FICOU DE FORA da última passada.
//
// A passada da agenda é PARCIAL por desenho (cripto sempre, ação só com o pregão
// aberto). O último conhecido funde por ATIVO, então a lista pode misturar uma
// cripto lida agora com uma ação lida às 17h de ontem — e sem isto as duas
// apareceriam com o mesmo peso, a segunda fingindo ser de agora.
//
// A pergunta que decide é "este ativo entrou na última passada?", respondida pela
// LISTA que a passada declara — não por comparação de horário. Duas varreduras
// podem cair no mesmo minuto, e aí a comparação diria "em dia" sobre um ativo que
// ninguém leu. Quem está na passada não ganha carimbo: repetir a mesma hora em
// vinte linhas é ruído, e o carimbo do topo já a diz.
//
// Sem hora conhecida (arquivo gravado antes de o carimbo por ativo existir) a
// linha diz que não foi varrido, em vez de calar — calar é o disfarce.
function scanCarimboDoAtivo(a) {
  const p = _scanData && _scanData.ultima_passada;
  if (!p || !a || !a.ticker) return null;      // varredura viva: tudo da mesma passada
  if ((p.tickers || []).indexOf(a.ticker) >= 0) return null;
  const c = a.gerado_em ? carimboDeInstante(new Date(a.gerado_em)) : null;
  const txt = c ? c.txt : "não varrido agora";
  const tit = c ? `este ativo não entrou na última passada — foi lido em ${c.txt}`
                : "este ativo não entrou na última passada, e a hora da leitura " +
                  "anterior dele não foi registrada";
  return `<span class="scan-tk-quando" title="${escapeHtml(tit)}">${escapeHtml(txt)}</span>`;
}

// A linha PERMANENTE que diz de quando é o que está na tela. Fica enquanto houver
// scan pintado — inclusive depois que a varredura nova chega, porque "a hora do
// scan exibido é obrigatória": o painel abre com o último resultado conhecido, e
// sem carimbo fixo não há como saber se o que se está lendo é de agora ou de
// terça. Quando o dado NÃO é de hoje o aviso deixa de ser discreto: palavra e
// peso, sem cor nova (DA-078 regra 3 — verde é ganho, vermelho é perda).
function renderScanCarimbo() {
  const el = $("scanCarimbo");
  if (!el) return;
  const c = scanCarimbo();
  if (!c) { el.innerHTML = ""; el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.classList.toggle("is-velho", c.velho);
  el.innerHTML = `<span class="sc-rot">scan de</span> <b>${escapeHtml(c.txt)}</b>` +
    (c.velho ? ` <span class="sc-alerta">não é de hoje</span>` : "") +
    scanCoberturaHtml();
}

// O QUE a última passada cobriu. Só aparece quando ela foi PARCIAL — uma varredura
// completa não precisa se explicar, e a linha viraria ruído fixo.
//
// A passada da agenda varre cripto sempre e ação só com o pregão aberto: de
// madrugada ela lê 8 de 20. Servir isso calado mostraria meia watchlist como se
// fosse a lista toda. Dizer "8 de 20" sem dizer POR QUE parece falha — e é regra —,
// então a sessão de mercado vem junto. Os outros 12 não somem: continuam na lista
// com o carimbo da passada em que foram lidos (:func:`scanCarimboDoAtivo`).
function scanCoberturaHtml() {
  const p = _scanData && _scanData.ultima_passada;
  if (!p || p.completa !== false) return "";
  const n = (p.tickers || []).length;
  const tot = p.universo || (_scanData.ativos || []).length;
  const porque = p.sessao && p.sessao !== "regular" && p.sessao !== "24h"
    ? ` (bolsa ${escapeHtml(SESSAO_PT[p.sessao] || p.sessao)})` : "";
  return `<span class="sc-cobertura">última passada leu <b>${n}</b> de ` +
    `<b>${tot}</b>${porque} — os demais trazem a hora deles</span>`;
}

// Vocabulário da sessão de mercado, do jeito que se lê em português. O valor cru
// vem do ``marketState`` do provedor (:mod:`live_price`), e "POSTPOST" na tela é
// vazamento de implementação. Chave desconhecida cai no próprio valor — que é feio,
// mas honesto, e melhor que inventar um estado que não se sabe.
const SESSAO_PT = {
  fechada: "fechada", closed: "fechada", regular: "aberta",
  pre: "pré-mercado", pos: "after-market", "24h": "24h",
  desconhecida: "sessão desconhecida",
};

async function runScan() {
  const btn = $("scanRunBtn");
  const ul = $("scanList");
  // O resultado anterior NÃO é destruído (task 014). Zerar a lista antes do fetch
  // deixava o painel VAZIO pelos 7-12s da varredura, e um scan que falhasse levava
  // junto o último resultado bom — o usuário perdia informação boa por causa de uma
  // atualização que nem chegou. Mesmo princípio do erro que preserva as etapas já
  // concluídas na tela de análise: não se descarta o que já se sabe.
  const temAnterior = !!_scanData;
  btn.disabled = true;
  btn.textContent = "escaneando…";
  if (temAnterior) {
    ul.classList.add("is-atualizando");
    // A HORA do dado exibido não se repete aqui: ela é PERMANENTE no carimbo logo
    // acima (`#scanCarimbo`), e o mesmo número com dois nomes na mesma tela é a
    // duplicata que a DA-077 proíbe. Esta linha diz só o que está acontecendo.
    scanNotice("atualizando… o que está na tela é o scan anterior");
  } else {
    // PRIMEIRA carga: não há o que preservar, então o texto de varredura fica.
    $("scanSummary").innerHTML = '<span class="hint">varrendo 1d + 4h + 1h…</span>';
    scanNotice("");
  }
  try {
    const res = await fetch("/api/scan?date=" + encodeURIComponent($("date").value || ""));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "falha no scan");
    _scanEstadoFilter = null;   // novo scan: limpa o filtro de estado anterior
    paintScan(data);
    scanNotice("");
  } catch (e) {
    const msg = escapeHtml(e.message);
    if (temAnterior) {
      // O anterior FICA, e o carimbo acima continua dizendo de quando ele é. O que
      // esta linha acrescenta — e só ela sabe — é que a tela NÃO se atualizou e
      // por quê: sem isso, um dado velho com carimbo velho parece só um carimbo
      // velho, e não uma varredura que falhou.
      scanNotice(`a atualização falhou (${msg}) — a tela continua no scan anterior`, true);
    } else {
      $("scanSummary").innerHTML = `<span class="error">${msg}</span>`;
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Escanear";
    ul.classList.remove("is-atualizando");
  }
}

// Chips de filtro por estado do gatilho. Reusa SCAN_ESTADO_PT pra cor/emoji.
// Clicar um chip seleciona o filtro; clicar de novo (ou o "Todos") desliga.
const SCAN_FILTER_ORDER = ["em_gatilho", "em_movimento", "invalidou", "formando", "sem_setup", "sem_dado"];
function renderScanFilters(s) {
  const host = $("scanFilters");
  if (!host) return;
  const hasAny = SCAN_FILTER_ORDER.some((k) => (s[k] || 0) > 0);
  if (!hasAny) { host.classList.add("hidden"); host.innerHTML = ""; return; }
  host.classList.remove("hidden");
  const chip = (key, label, n) => {
    const active = _scanEstadoFilter === key;
    // A COR do chip vem da classe `scan-filter ${key}` — era ela e a bolinha
    // colorida juntas; sem o pictograma (DA-076) ela faz o trabalho sozinha, com
    // o rótulo do estado escrito ao lado.
    const cls = `scan-filter ${key}${active ? " is-active" : ""}`;
    return `<button type="button" class="${cls}" data-filter="${key}">${escapeHtml(label)} <b>${n}</b></button>`;
  };
  // chip "Todos" pra desligar o filtro (só aparece quando há um ativo)
  const allChip = _scanEstadoFilter
    ? `<button type="button" class="scan-filter all" data-filter="">Todos</button>`
    : "";
  host.innerHTML = allChip + SCAN_FILTER_ORDER
    .filter((k) => (s[k] || 0) > 0)
    .map((k) => {
      // em_gatilho é direção-aware no chip de linha ({compra,venda}), mas no
      // filtro é genérico — achatamos pra um label único. Os demais são [label].
      const entry = SCAN_ESTADO_PT[k];
      // O filtro agrega compra E venda, então a DIREÇÃO não cabe nele — e é por
      // isso que aqui vai a FASE: "NA ENTRADA", a mesma palavra da seção de
      // Sinais e do card. Era "EM GATILHO", a última sobra de um quinto jeito de
      // nomear o mesmo momento (DA-121).
      const [label] = (k === "em_gatilho") ? [faseRotulo("agora")] : entry;
      return chip(k, label, s[k] || 0);
    }).join("");
  host.querySelectorAll("[data-filter]").forEach((b) => {
    b.addEventListener("click", () => {
      const f = b.dataset.filter;
      _scanEstadoFilter = f || null;
      if (_scanData) paintScan(_scanData);
    });
  });
}

// Busca + modo de apresentação (pedido do Samyr). A busca filtra por SIGLA — é
// assim que se procura um papel numa lista de 20 × 3 frames. O modo fica guardado
// no navegador: quem prefere a lista densa não escolhe de novo a cada scan.
const _SCAN_VIEW_KEY = "td_scan_view";
const _SCAN_VIEWS = ["sinais", "cards", "lista"];
let _scanBusca = "";
// SINAIS é a entrada padrão (DA-117). Quem já escolheu um modo mantém o dele — a
// escolha do usuário é dele (mesma disciplina da DA-089); quem nunca escolheu
// cai na visão de DECISÃO em vez da de dado.
let _scanView = (() => {
  try {
    const v = localStorage.getItem(_SCAN_VIEW_KEY);
    return _SCAN_VIEWS.indexOf(v) >= 0 ? v : "sinais";
  } catch (e) { return "sinais"; }
})();

// Timeframe no INÍCIO da linha, na MESMA gramática da barra de controle: pill
// estreita, mono, rótulo curto (S · D · 4h · 1h · 30m). Duas correções em cima da
// mesma queixa: primeiro ele saía colado no preço ("1d$513,530.15%"); depois, com
// caixa própria, ficou gordo e repetitivo — um chip largo por linha, competindo com
// o ativo. Aqui ele volta a ser o que é: uma etiqueta discreta, e o ATIVO é quem
// tem destaque.
// O curto do scan vem da MESMA fonte da barra e do gráfico (TF_SHORT, derivado de
// ALL_TFS). Era aqui que morava a lista paralela mantida à mão — foi ela que
// deixou a barra de controle pra trás quando a task 012 encurtou só o scan.
// Frame que a fonte não conhece cai no próprio código, que já é curto.
function scanTfBadge(frame) {
  const curto = tfCurto(frame) || "—";
  return `<span class="scan-tf" title="${escapeHtml(frame || "")}">${escapeHtml(curto)}</span>`;
}

// Níveis de uma linha do scan (gatilho · SL · TP · R:R), ou o MOTIVO quando o
// servidor recusou o alvo. Compartilhado pelos dois modos de apresentação.
// Linha do STORM no modo CARDS. Aqui há espaço pra nomear as coisas (não há
// cabeçalho de coluna), então ela diz o SETUP, a entrada usada e os níveis dela —
// e, quando o Éden veta, diz "não opera" com o motivo em vez de um nível que a
// regra proíbe operar.
function scanStormLinhaHtml(f) {
  const st = f.storm || {};
  const e = st.estado;
  if (!e || e === "sem_setup" || e === "sem_dado") return "";
  if (!st.opera) {
    return `<div class="scan-levels scan-storm-linha vetado">` +
      `<span class="ss-tag">Storm123</span>` +
      `<span class="scan-note" title="${escapeHtml(st.veto || "")}">não opera — Éden desalinhado</span>` +
      `</div>`;
  }
  const n = st.entrada === "ponto3" ? "ponto 3" : st.entrada === "ponto2" ? "ponto 2" : "pontos 2 e 3";
  return `<div class="scan-levels scan-storm-linha">` +
    `<span class="ss-tag">Storm123</span>` +
    `<span class="ss-ent">entrada no ${escapeHtml(n)}${st.ordem ? ` · ${escapeHtml(st.ordem)}` : ""}</span>` +
    `<span>gatilho <b>${scanFmt(st.trigger)}</b></span>` +
    `<span>SL <b>${scanFmt(st.sl)}</b></span>` +
    (st.tp != null ? `<span>TP <b>${scanFmt(st.tp)}</b></span>` : "") +
    (st.rr != null ? `<span>R:R <b>${scanFmt(st.rr)}</b></span>` : "") +
    `</div>`;
}

function scanLevelsHtml(f) {
  const hasLevels = (f.estado === "em_gatilho" || f.estado === "em_movimento") && f.trigger != null;
  if (!hasLevels) {
    return ((f.estado === "invalidou" && f.invalidacao != null)
      ? `<div class="scan-levels"><span class="scan-note">invalidação <b>${scanFmt(f.invalidacao)}</b> — premissa rompida</span></div>`
      : "") + scanStormLinhaHtml(f);
  }
  return `<div class="scan-levels">` +
    `<span>gatilho <b>${scanFmt(f.trigger)}</b></span>` +
    `<span>SL <b>${scanFmt(f.sl)}</b></span>` +
    // Sem alvo publicável, mostra o MOTIVO (o que a tela de análise já faz) em
    // vez de um TP que o servidor recusou — antes vinha "TP 512,76" ao lado de
    // "gatilho 512,76 · R:R não calculável", e o porquê era descartado.
    (f.tp != null
      ? `<span>TP <b>${scanFmt(f.tp)}</b></span>` + scanRrHtml(f)
      : `<span class="scan-note">sem alvo — ${escapeHtml(f.rr_note || "nível de alvo indefinido")}</span>`) +
    `</div>` + scanStormLinhaHtml(f);
}

// ---- modo LISTA: uma TABELA de verdade ------------------------------------
// "Faz colunas mais definidas e deixa cada informação em uma coluna" — o pedido é
// COMPARAR descendo a coluna. A lista era `flex-wrap` com `margin-left: auto`, e
// isso alinha cada linha pelo PRÓPRIO conteúdo: o gatilho do MSFT, do LINK-USD e do
// ZEC-USD começavam em três lugares diferentes. Aqui cada linha é uma GRADE com o
// MESMO template, então a coluna existe de verdade.
//
// Os rótulos por célula (gatilho, SL, TP) saem: quem diz o que é cada
// coluna é o CABEÇALHO, e a célula fica com o número — é isso que faz a coluna ser
// lida como coluna. No modo CARDS os rótulos ficam (lá não há cabeçalho).
const SCAN_COLUNAS = [
  ["tf", "Timeframe do candle"],
  ["ativo", "Sigla do ativo"],
  ["preço", "Preço no fechamento do candle"],
  ["dist", "Distância do preço até o gatilho"],
  ["estado", "Estado do setup 1-2-3"],
  ["gatilho", "Nível que aciona a entrada"],
  ["SL", "Stop loss"],
  ["TP", "Alvo publicável — ou o motivo de não haver"],
  ["R:R", "Risco/retorno"],
  // O STORM é outro SETUP (DA-081), não outra leitura deste: outro detector, outro
  // ponto 2, outro stop, outro alvo, e um filtro com poder de VETO. Por isso ele
  // ganha COLUNA — some-lo às células do 1-2-3 misturaria dois métodos no mesmo
  // número, que é exatamente o que a task 008 provou não descrever trade nenhum.
  ["Storm123", "O 1-2-3 do Stormer + filtro Éden — setup DIFERENTE do Setup123 desta lista"],
];

function scanCabecalhoHtml() {
  return `<li class="scan-line-head" aria-hidden="false">` + SCAN_COLUNAS.map(([nome, ajuda]) =>
    `<span class="scan-col" title="${escapeHtml(ajuda)}">${escapeHtml(nome)}</span>`).join("") + `</li>`;
}

// O R:R medido do PREÇO ATUAL (setup já acionado) muda a leitura do número, e o
// qualificador da task 012 ("do preço atual") não cabe na célula sem empurrar a
// coluna. Vira marcador + title + legenda embaixo da tabela: forma curta na célula,
// texto inteiro a um passo — nunca truncado calado.
function scanRrDoPrecoAtual(f) {
  return f.rr != null && !f.rr_residual && f.pattern_state === "acionado";
}

// As quatro células de nível da LISTA (gatilho · SL · TP/motivo · R:R). Célula sem
// dado fica VAZIA — na tabela ela não pode sumir, senão a coluna seguinte sobe de
// posição e o alinhamento (que é o pedido inteiro) morre.
// O nome da coluna vai JUNTO da célula, escondido enquanto a tabela é tabela: na
// largura em que a grade não cabe e a linha volta a quebrar, ele reaparece — número
// solto sem cabeçalho em cima não diz nada. De quebra é o que dá contexto a quem usa
// leitor de tela (isto é `ul`/`li`, não `table`: não há associação automática entre
// cabeçalho e célula).
function scanCk(nome) {
  return `<span class="scan-ck">${escapeHtml(nome)}</span>`;
}

function scanLineCellsHtml(f) {
  const vazia = `<span class="scan-cell"></span>`;
  const hasLevels = (f.estado === "em_gatilho" || f.estado === "em_movimento") && f.trigger != null;
  if (!hasLevels) {
    // Invalidou: o nível que importa é a INVALIDAÇÃO — não é gatilho nem SL, então
    // vai na coluna do motivo (a flexível), com o porquê no title.
    const motivo = (f.estado === "invalidou" && f.invalidacao != null)
      ? `<span class="scan-cell scan-motivo" title="premissa rompida — o preço perdeu o nível de invalidação">` +
        scanCk("invalidação") + `<b>${scanFmt(f.invalidacao)}</b></span>`
      : vazia;
    return vazia + vazia + motivo + vazia + scanStormCellHtml(f);
  }
  const tp = f.tp != null
    ? `<span class="scan-cell num">${scanCk("TP")}<b>${scanFmt(f.tp)}</b></span>`
    : `<span class="scan-cell scan-note" title="${escapeHtml("sem alvo — " + (f.rr_note || "nível de alvo indefinido"))}">sem alvo</span>`;
  return `<span class="scan-cell num">${scanCk("gatilho")}<b>${scanFmt(f.trigger)}</b></span>` +
    `<span class="scan-cell num">${scanCk("SL")}<b>${scanFmt(f.sl)}</b></span>` + tp +
    scanRrCellHtml(f) + scanStormCellHtml(f);
}

// Rótulo curto do estado do Storm na célula da tabela. "vetado" é o estado que só
// ele tem: o Éden proíbe operar, e isso não é "formando" nem "sem setup".
// Forma CURTA de propósito: a célula divide 96px com a entrada (p2/p3) e o R:R, e
// rótulo que só cabe truncado ("EM MOVIMEN…") não informa. O cabeçalho da coluna diz
// que aquilo é o Storm, e o `title` da célula leva o estado por extenso.
const SCAN_STORM_ESTADO = {
  em_gatilho: "gatilho", em_movimento: "movimento",
  // ZONA NEUTRA (task 016): opera, mas na região entre a MME 8 e a MME 80, que o
  // Stormer chama de perigosa. Estado PRÓPRIO na lista — mostrá-la como "gatilho"
  // igual às outras é onde o aviso se perderia.
  zona_neutra: "zona neutra",
  formando: "formando", vetado: "vetado", sem_setup: "sem setup", sem_dado: "sem dado",
};

function scanStormCellHtml(f) {
  const st = f.storm || {};
  const e = st.estado || "sem_dado";
  if (e === "sem_setup" || e === "sem_dado") {
    return `<span class="scan-cell scan-storm vazio" title="${escapeHtml(st.motivo || "sem 1-2-3 Storm neste frame")}">—</span>`;
  }
  // O que cabe numa célula: o estado, QUAL entrada (p2/p3 — são duas leituras do
  // mesmo padrão) e o R:R. Os dois gatilhos, o stop e os dois alvos vão no title:
  // a coluna responde "está para acontecer e paga?", o resto é a análise.
  const ent = st.entrada === "ponto3" ? "p3" : st.entrada === "ponto2" ? "p2" : "p2/3";
  const rr = st.rr != null ? `${scanFmt(st.rr)}` : "—";
  const detalhe = [
    // O rótulo vem PRONTO do scanner (mesmo vocabulário do card): a célula não
    // reescreve nome de estado. Sem ele (linha antiga em cache), degrada pro que
    // aquela linha sempre mostrou em vez de inventar um nome.
    `Éden: ${st.eden_rotulo || (st.eden_ok ? st.eden || "alinhado" : "desalinhado — não opera")}`,
    st.qualidade ? `qualidade ${st.qualidade}` : "",
    st.veto || "",
    ...(st.leituras || []).map((L) => {
      const n = L.entrada === "ponto3" ? "ponto 3" : L.entrada === "ponto2" ? "ponto 2" : "pontos 2 e 3";
      return `entrada no ${n} (${L.ordem || ""}): gatilho ${scanFmt(L.trigger)} · alvo ${scanFmt(L.tp)} · R:R ${L.rr != null ? scanFmt(L.rr) : "—"}`;
    }),
    st.sl != null ? `stop ${scanFmt(st.sl)} (ponto 2, comum às duas)` : "",
  ].filter(Boolean).join("\n");
  const cls = ["scan-cell", "scan-storm", e, st.direction || ""].filter(Boolean).join(" ");
  return `<span class="${cls}" title="${escapeHtml(detalhe)}">` +
    `<span class="ss-e">${escapeHtml(SCAN_STORM_ESTADO[e] || e)}</span>` +
    `<span class="ss-p">${escapeHtml(ent)}</span>` +
    `<b class="ss-rr">${rr}</b></span>`;
}

// A frase do R:R de um setup ACIONADO, escrita uma vez pra célula e pra linha
// detalhada. Ela carrega os DOIS números: o que sobra agora e o que o setup
// oferecia no gatilho. Sem o segundo, um 0,09 lê-se como "o método dá trade ruim";
// com ele, lê-se "cheguei tarde" — que é o que de fato aconteceu.
function scanRrPercursoTxt(f) {
  const base = "R:R medido do PREÇO ATUAL — o setup já foi acionado, então o "
    + "número mede o que ainda sobra do trade";
  const andou = f.andado_pct != null
    ? `; o preço já andou ${Math.round(f.andado_pct)}% do caminho até o alvo` : "";
  const gat = f.rr_gatilho != null
    ? `; no gatilho o setup oferecia ${Number(f.rr_gatilho).toFixed(2)}:1` : "";
  return base + andou + gat;
}

function scanRrCellHtml(f) {
  // Sem R:R o motivo já está na coluna do TP ("sem alvo — …"); aqui a célula diz
  // que não há número, com o porquê no title. Numa tabela a célula não pode sumir.
  if (f.rr == null) {
    return `<span class="scan-cell num vazio" title="${escapeHtml("R:R não calculável — " + (f.rr_note || "sem alvo publicável"))}">` +
      scanCk("R:R") + `—</span>`;
  }
  if (f.rr_residual) {
    const sobra = (f.rr_retorno != null && f.rr_risco != null)
      ? ` — sobrou ${scanFmt(f.rr_retorno)} pra ${scanFmt(f.rr_risco)} de risco` : "";
    return `<span class="scan-cell scan-note" title="${escapeHtml("alvo praticamente alcançado" + sobra)}">no alvo</span>`;
  }
  const marca = scanRrDoPrecoAtual(f)
    ? `<span class="scan-mark" title="${escapeHtml(scanRrPercursoTxt(f))}">*</span>` : "";
  return `<span class="scan-cell num">${scanCk("R:R")}<b>${f.rr.toFixed(2)}</b>${marca}</span>`;
}

// O R:R e — quando muda a leitura — a BASE da entrada.
//
// Num setup JÁ ACIONADO a entrada de referência é o PREÇO ATUAL, não o gatilho: o
// R:R passa a medir o que AINDA sobra do trade. Correto, e enganoso escrito como
// número seco. MSFT 1h (29/08): "gatilho 497,14 · TP 513,73 · R:R 0.00" com o preço
// em 513,67 — 0.00 lê-se "setup sem retorno", quando a verdade é "já andou, sobrou
// 0,06 pra 28,70 de risco" (medido do gatilho daria 1,36). O número não muda; o que
// muda é a tela DIZER o que ele significa.
function scanRrHtml(f) {
  if (f.rr == null) return `<span>R:R <b>não calculável</b></span>`;
  if (f.rr_residual) {
    const sobra = (f.rr_retorno != null && f.rr_risco != null)
      ? ` <span class="scan-sub">sobrou ${scanFmt(f.rr_retorno)} pra ${scanFmt(f.rr_risco)} de risco</span>` : "";
    return `<span class="scan-note">alvo praticamente alcançado${sobra}</span>`;
  }
  // Acionado sem ser residual: o número vale, mas a base precisa estar dita — e ao
  // lado do que o setup oferecia NO GATILHO, que é o que separa "método ruim" de
  // "perdi a entrada".
  if (f.pattern_state === "acionado") {
    const gat = f.rr_gatilho != null
      ? ` <span class="scan-sub">no gatilho ${Number(f.rr_gatilho).toFixed(2)}</span>` : "";
    const andou = f.andado_pct != null
      ? ` <span class="scan-sub">andou ${Math.round(f.andado_pct)}%</span>` : "";
    return `<span title="${escapeHtml(scanRrPercursoTxt(f))}">R:R <b>${f.rr.toFixed(2)}</b>` +
      ` <span class="scan-sub">do preço atual</span>${gat}${andou}</span>`;
  }
  return `<span>R:R <b>${f.rr.toFixed(2)}</b></span>`;
}

// A legenda do "*" só aparece quando alguma linha o usa — legenda de marcador que
// não está na tela é ruído.
function scanLegenda(temMarca) {
  const el = $("scanLegenda");
  if (!el) return;
  el.innerHTML = temMarca
    ? `<span class="scan-mark">*</span> R:R medido do <b>preço atual</b> — o setup já foi acionado, ` +
      `então o número mede o que ainda sobra do trade (medido do gatilho daria outro valor).`
    : "";
  el.classList.toggle("hidden", !temMarca);
}

function scanActionsHtml(ticker, f) {
  if (f.estado !== "em_gatilho") return "";
  return `<div class="scan-actions-row">` +
    `<button type="button" class="scan-go" data-go="${escapeHtml(ticker)}|${escapeHtml(f.frame)}|padrao">Analisar Padrão</button>` +
    `<button type="button" class="scan-go erick" data-go="${escapeHtml(ticker)}|${escapeHtml(f.frame)}|erick">Analisar Erick</button>` +
    `</div>`;
}


// ================= SINAIS DE ENTRADA (DA-117) ==================================
// A unidade é a OPORTUNIDADE — ativo + método + direção, com os frames que
// concordam agregados —, não o par ativo×frame da tabela. O cálculo inteiro
// (confluência, conflito, janela derivada do R:R mínimo) vive no servidor
// (`webui/sinais.py`) e chega pronto em `data.oportunidades`: reimplementá-lo aqui
// criaria duas definições de "vale entrar", e a que o usuário lê seria a errada.

// SEÇÕES na ordem em que se decide. O conflito fica por último de propósito: ele
// não é entrada, e não pode competir por atenção com quem é.
// Os títulos são as palavras do EIXO (DA-121) — as MESMAS da lateral e do scan.
// A nota de cada seção acrescenta o que só ela sabe (a janela), sem redefinir a
// fase: o leitor aprende quatro palavras uma vez e as reconhece em toda a tela.
const SINAL_SECOES = [
  { key: "entrada", fase: "agora", titulo: "Na entrada",
    nota: "o preço está DENTRO da janela em que o retorno ainda paga o risco" },
  { key: "a_caminho", fase: "esperando", titulo: "Aguardando",
    nota: "o gatilho ainda não veio — o padrão existe e o preço não chegou nele" },
  { key: "passou", fase: "andou", titulo: "Já andou",
    nota: "acionou e o preço passou da entrada — agora não paga o risco, ou nunca pagou" },
  { key: "conflito", fase: null, titulo: "Conflito entre frames",
    nota: "os frames do mesmo método discordam da direção — não há fase a apontar, "
          + "porque não há um lado a operar" },
];

const SINAL_DIR_PT = { compra: "COMPRA", venda: "VENDA" };

// NOVO DESDE A ÚLTIMA VISITA. O sinal que apareceu agora é a informação mais
// perecível da tela — e a que se perde num painel que se repinta igual.
//
// A memória é do NAVEGADOR (localStorage), não do servidor: "desde a última vez
// que EU olhei" é por pessoa, e o scan é público. A chave carrega o GATILHO
// (mesma família da de-duplicação do ledger): um padrão que morreu e outro que
// nasceu no mesmo ativo e direção são sinais diferentes, e sem o gatilho na chave
// o segundo nunca se anunciaria.
const _SINAIS_VISTOS_KEY = "td_sinais_vistos";
const _SINAIS_VALIDADE_MS = 7 * 24 * 3600 * 1000;
let _sinaisNovos = new Set();

function _lerVistos() {
  try { return JSON.parse(localStorage.getItem(_SINAIS_VISTOS_KEY) || "{}") || {}; }
  catch (e) { return {}; }
}

// Calcula o que é novo E registra a visita. Roda UMA vez por payload: repintar
// (filtro, busca, troca de modo) não pode apagar as marcas do que acabou de
// chegar — foi para isso que a marcação saiu do render.
function marcarSinaisNovos(ops) {
  const vistos = _lerVistos();
  const primeira = Object.keys(vistos).length === 0;
  const agora = Date.now();
  const novos = new Set();
  (ops || []).forEach((o) => {
    // PRIMEIRA visita não marca nada: com a memória vazia, "novo" seria a lista
    // inteira — um alarme que não distingue nada de nada.
    if (!primeira && !(o.chave in vistos)) novos.add(o.chave);
    vistos[o.chave] = agora;
  });
  Object.keys(vistos).forEach((k) => {
    if (agora - vistos[k] > _SINAIS_VALIDADE_MS) delete vistos[k];
  });
  try { localStorage.setItem(_SINAIS_VISTOS_KEY, JSON.stringify(vistos)); }
  catch (e) { /* quota: perde-se a memória, não a tela */ }
  _sinaisNovos = novos;
}

function sinalPreco(v) {
  return v == null ? "—" : Number(v).toLocaleString("pt-BR", { maximumFractionDigits: 2 });
}

// A JANELA em palavras. O limite não vai sozinho: um número sem o porquê vira
// mais um nível pra decorar, e o porquê é a coisa nova que esta tela traz.
function sinalJanelaHtml(o) {
  const j = o.janela;
  if (!j) {
    return `<div class="sn-janela sem"><span class="sn-rot">janela</span>` +
      `<span class="sn-motivo">sem alvo publicado — não dá pra medir até onde ainda paga</span></div>`;
  }
  if (!j.existe) {
    return `<div class="sn-janela sem"><span class="sn-rot">janela</span>` +
      `<span class="sn-motivo">${escapeHtml(j.motivo)}</span></div>`;
  }
  const cls = (j.estado === "aberta" ? "aberta" : j.estado === "fechada" ? "fechada" : "espera") +
    ` ${escapeHtml(o.direcao || "")}`;
  // O LADO ruim do limite depende da direção, e trocá-lo inverte o conselho: na
  // compra entrar mais CARO piora o R:R; na venda, entrar mais BARATO. Uma frase
  // só, com "acima"/"abaixo" fixo, dizia o contrário do certo em metade dos cards.
  const venda = o.direcao === "venda";
  const ladoRuim = venda ? "abaixo" : "acima";
  const nota = j.estado === "aberta"
    ? `${ladoRuim} de <b>${sinalPreco(j.limite)}</b> o retorno não paga o risco`
    : j.estado === "fechada"
      ? `o preço ${venda ? "caiu abaixo" : "passou"} de <b>${sinalPreco(j.limite)}</b> — ` +
        `entrar agora não paga o risco`
      : `abre quando o preço tocar <b>${sinalPreco(j.gatilho)}</b>`;
  const larg = j.largura_pct != null
    ? `<span class="sn-larg" title="largura da janela em % do gatilho">${pctBR(j.largura_pct * 100)}%</span>`
    : "";
  return `<div class="sn-janela ${cls}"><span class="sn-rot">janela</span>` +
    `<b class="sn-faixa">${sinalPreco(j.de)} a ${sinalPreco(j.ate)}</b>${larg}` +
    `<span class="sn-motivo">${nota}</span></div>`;
}

function sinalNiveisHtml(o) {
  if (o.gatilho == null) return "";
  return `<div class="sn-niveis">` +
    `<span>gatilho <b>${sinalPreco(o.gatilho)}</b></span>` +
    (o.sl != null ? `<span>SL <b>${sinalPreco(o.sl)}</b></span>` : "") +
    (o.tp != null ? `<span>TP <b>${sinalPreco(o.tp)}</b></span>` : "") +
    (o.rr_gatilho != null
      ? `<span title="R:R de quem entra exatamente no gatilho">R:R no gatilho <b>${o.rr_gatilho.toFixed(2)}</b></span>`
      : "") +
    (o.preco != null ? `<span class="sn-agora">preço <b>${sinalPreco(o.preco)}</b></span>` : "") +
    `</div>`;
}

// CONFLUÊNCIA de relance: os frames que concordam, escritos, e quantos são. É o
// cruzamento de linhas que a tabela obrigava a fazer com o olho.
function sinalFramesHtml(o) {
  const chips = (o.frames || []).map(
    (f) => `<span class="sn-frame">${escapeHtml(tfCurto(f) || f)}</span>`).join("");
  const n = o.confluencia || 0;
  const quantos = n > 1
    ? `<span class="sn-conf forte">${n} frames concordam</span>`
    : `<span class="sn-conf">1 frame</span>`;
  return `<div class="sn-frames">${chips}${quantos}</div>`;
}

function sinalDissidentesHtml(o) {
  const d = o.dissidentes || [];
  if (!d.length) return "";
  const txt = d.map((x) => `${escapeHtml(tfCurto(x.frame) || x.frame)} tinha ` +
    `${escapeHtml(SINAL_DIR_PT[x.direcao] || x.direcao)}, invalidado`).join(" · ");
  return `<div class="sn-dissidente">${txt}</div>`;
}

function sinalOutroMetodoHtml(o) {
  const m = o.outro_metodo;
  if (!m || !m.direcao || m.direcao === o.direcao) return "";
  return `<div class="sn-outro">o <b>${escapeHtml(m.metodo_rotulo)}</b> lê ` +
    `${escapeHtml(SINAL_DIR_PT[m.direcao] || m.direcao)} neste ativo</div>`;
}

function sinalConflitoHtml(o) {
  const lados = (o.lados || []).map((l) =>
    `<div class="sn-lado"><b>${escapeHtml(SINAL_DIR_PT[l.direcao] || l.direcao)}</b>` +
    `<span class="sn-lado-frames">${(l.frames || []).map(
      (f) => `<span class="sn-frame">${escapeHtml(tfCurto(f) || f)}</span>`).join("")}</span></div>`).join("");
  return `<div class="sn-conflito">${lados}` +
    `<div class="sn-motivo">os frames do mesmo método apontam para lados opostos — ` +
    `sem níveis aqui, porque não há um lado a operar</div></div>` +
    sinalDissidentesHtml(o);
}

function sinalCardHtml(o) {
  const novo = _sinaisNovos.has(o.chave)
    ? `<span class="sn-novo" title="apareceu desde a última vez que você olhou">NOVO</span>` : "";
  const dir = o.direcao
    ? `<span class="sn-dir ${escapeHtml(o.direcao)}">${escapeHtml(SINAL_DIR_PT[o.direcao] || o.direcao)}</span>`
    : `<span class="sn-dir conflito">CONFLITO</span>`;
  const corpo = o.estado === "conflito"
    ? sinalConflitoHtml(o)
    : sinalFramesHtml(o) + sinalJanelaHtml(o) + sinalNiveisHtml(o) +
      sinalDissidentesHtml(o) + sinalOutroMetodoHtml(o) +
      (o.aviso ? `<div class="sn-aviso">${escapeHtml(o.aviso)}</div>` : "") +
      (o.estado === "entrada" ? sinalAcoesHtml(o) : "");
  return `<li class="sn-card ${escapeHtml(o.estado)} ${escapeHtml(o.direcao || "")}">` +
    `<div class="sn-head">` +
    `<b class="sn-tk" data-open="${escapeHtml(o.ticker)}|${escapeHtml(o.frame_lider || "")}">${escapeHtml(o.ticker)}</b>` +
    dir +
    `<span class="sn-metodo">${escapeHtml(o.metodo_rotulo)}</span>` + novo +
    `</div>` + corpo + `</li>`;
}

function sinalAcoesHtml(o) {
  const f = o.frame_lider || "";
  return `<div class="scan-actions-row">` +
    `<button type="button" class="scan-go" data-go="${escapeHtml(o.ticker)}|${escapeHtml(f)}|padrao">Analisar Padrão</button>` +
    `<button type="button" class="scan-go erick" data-go="${escapeHtml(o.ticker)}|${escapeHtml(f)}|erick">Analisar Erick</button>` +
    `</div>`;
}

// A lista de SINAIS, em seções. Seção vazia não aparece — cabeçalho sem conteúdo
// embaixo é ruído, e a ausência já está dita pelo que sobrou na tela.
function renderSinais(ul, ops) {
  const busca = _scanBusca;
  const vis = (ops || []).filter((o) => !busca || (o.ticker || "").toUpperCase().includes(busca));
  if (!vis.length) {
    ul.innerHTML = `<li class="scan-vazio">` + (ops && ops.length
      ? `nada casa com <b>${escapeHtml(busca)}</b>`
      : `nenhuma leitura viva na watchlist — todos os padrões invalidados, sem setup ou sem dado`) +
      `</li>`;
    return;
  }
  ul.innerHTML = SINAL_SECOES.map((sec) => {
    const doGrupo = vis.filter((o) => o.estado === sec.key);
    if (!doGrupo.length) return "";
    const ajuda = sec.fase ? faseAjuda(sec.fase) : "";
    return `<li class="sn-secao"><span class="sn-secao-tit"` +
      (ajuda ? ` title="${escapeHtml(ajuda)}"` : "") +
      `>${escapeHtml(sec.titulo)}</span>` +
      `<span class="sn-secao-n">${doGrupo.length}</span>` +
      `<span class="sn-secao-nota">${escapeHtml(sec.nota)}</span></li>` +
      doGrupo.map(sinalCardHtml).join("");
  }).join("");
}

// Liga a busca e o alternador de modo UMA vez (a lista é repintada a cada filtro).
let _scanToolsBound = false;
function bindScanTools() {
  if (_scanToolsBound) return;
  _scanToolsBound = true;
  const busca = $("scanSearch");
  if (busca) busca.addEventListener("input", () => {
    _scanBusca = busca.value.trim().toUpperCase();
    if (_scanData) paintScan(_scanData);
  });
  document.querySelectorAll(".scan-view").forEach((b) => b.addEventListener("click", () => {
    _scanView = _SCAN_VIEWS.indexOf(b.dataset.view) >= 0 ? b.dataset.view : "sinais";
    try { localStorage.setItem(_SCAN_VIEW_KEY, _scanView); } catch (e) { /* quota */ }
    if (_scanData) paintScan(_scanData);
  }));
}

function paintScan(data) {
  // Carimba a hora só quando o dado é NOVO: filtro, busca e troca de modo
  // re-pintam com a MESMA referência e não podem rejuvenescer o carimbo. E o
  // carimbo é o do SERVIDOR (`gerado_em`), não o relógio de quando chegou —
  // senão o scan lido do disco na abertura nasceria com a hora de agora.
  const dadoNovo = data !== _scanData;
  if (dadoNovo) _scanAt = scanQuando(data);
  // O "novo desde a última visita" é calculado UMA vez por payload. Fazê-lo no
  // render apagaria as marcas ao trocar de filtro ou de modo — e a marca some
  // justamente quando o usuário está olhando.
  if (dadoNovo) marcarSinaisNovos(data.oportunidades);
  _scanData = data;   // guarda pra re-pintar ao trocar o filtro de estado
  const s = data.resumo || {};
  // O RESUMO também fala o eixo (DA-121). Era a linha mais visível da tela e a
  // última a manter o vocabulário antigo — "em gatilho · em movimento · invalidou
  // · formando", quatro palavras que a seção logo abaixo já dizia de outro jeito.
  const fr = (f) => faseRotulo(f).toLowerCase();
  $("scanSummary").innerHTML =
    `<b>${s.em_gatilho || 0}</b> ${fr("agora")} · <b>${s.em_movimento || 0}</b> ${fr("andou")} · ` +
    `<b>${s.invalidou || 0}</b> ${fr("morreu")} · <b>${s.formando || 0}</b> ${fr("esperando")} · ` +
    `${s.sem_setup || 0} sem setup · ${s.sem_dado || 0} sem dado` +
    (data.date ? `<span class="hint"> — ${escapeHtml(data.date)} · ${escapeHtml((data.frames || []).join(" + "))}</span>` : "");
  // De QUANDO é o que está na tela. Fixo, ao lado do resumo: o painel abre com o
  // último scan salvo, e sem isto não haveria como distinguir a varredura de
  // agora da de ontem.
  renderScanCarimbo();
  // Chips de filtro por estado: cada um mostra a contagem; clicar filtra.
  renderScanFilters(s);
  bindScanTools();
  const tools = $("scanTools");
  if (tools) tools.classList.toggle("hidden", !(data.ativos || []).length);
  document.querySelectorAll(".scan-view").forEach((b) =>
    b.classList.toggle("is-active", b.dataset.view === _scanView));
  const ul = $("scanList");
  ul.classList.toggle("is-lista", _scanView === "lista");
  ul.classList.toggle("is-sinais", _scanView === "sinais");
  if (_scanView === "sinais") {
    // Os chips de estado são do DADO (em gatilho / formando / invalidou). Deixá-los
    // ligados aqui daria dois jeitos de esconder a mesma linha, com vocabulários
    // diferentes — o das seções e o dos chips.
    const chips = $("scanFilters");
    if (chips) { chips.classList.add("hidden"); }
    // A visão de DECISÃO não usa o filtro por estado do gatilho (que é do dado):
    // aqui a organização é por seção — entrada, a caminho, fora da janela,
    // conflito —, e um segundo filtro por cima dela só criaria dois jeitos de
    // esconder a mesma linha.
    renderSinais(ul, data.oportunidades);
    scanLegenda(false);
    ligaAcoesDoScan(ul);
    return;
  }
  // Filtro de estado (chips) + busca por sigla. Os dois se somam.
  const ativos = (data.ativos || []).filter((a) =>
    (!_scanEstadoFilter || (a.melhor || {}).estado === _scanEstadoFilter) &&
    (!_scanBusca || (a.ticker || "").toUpperCase().includes(_scanBusca)));

  if (_scanView === "lista") {
    // LISTA: uma linha por ativo+frame, sem agrupar. Densa de propósito — é o modo
    // de varrer o portfólio inteiro de relance, sem rolar 20 cards.
    const linhas = [];
    let temMarca = false;
    ativos.forEach((a) => (a.frames || []).filter((f) => f.estado !== "sem_dado").forEach((f) => {
      if (scanRrDoPrecoAtual(f)) temMarca = true;
      linhas.push(
        `<li class="scan-line-row ${f.estado} ${f.direction || ""}" data-open="${escapeHtml(a.ticker)}|${escapeHtml(f.frame)}">` +
        scanTfBadge(f.frame) +
        `<b class="scan-tk-inline">${escapeHtml(a.ticker)}</b>` +
        (scanCarimboDoAtivo(a) || "") +
        `<span class="scan-price">$${scanFmt(f.price)}</span>` +
        `<span class="scan-dist">${f.dist_txt || "—"}</span>` +
        scanEstadoChip(f.estado, f.direction, f.andado_pct) +
        scanLineCellsHtml(f) + `</li>`);
    }));
    // O cabeçalho só existe se houver tabela embaixo dele.
    ul.innerHTML = linhas.length
      ? scanCabecalhoHtml() + linhas.join("")
      : `<li class="scan-vazio">nada casa com <b>${escapeHtml(_scanBusca || "o filtro")}</b></li>`;
    scanLegenda(temMarca);
  } else {
    ul.innerHTML = ativos.map((a) => {
      // Cada ativo reporta TODOS os frames (1d, 4h, 1h) — um por linha, com seu
      // próprio estado e direção. Sem hierarquia, sem escolher "melhor": mostra
      // todos e o timeframe de cada. O ticker cabeça agrupa; cada sub-linha abre.
      const valid = (a.frames || []).filter((f) => f.estado !== "sem_dado");
      const rows = valid.map((f) =>
        `<div class="scan-frame-row ${f.estado} ${f.direction || ""}" data-open="${escapeHtml(a.ticker)}|${escapeHtml(f.frame)}">` +
        scanTfBadge(f.frame) +
        `<span class="scan-price">$${scanFmt(f.price)}</span>` +
        `<span class="scan-dist">${f.dist_txt || "—"}</span>` +
        scanEstadoChip(f.estado, f.direction, f.andado_pct) +
        scanLevelsHtml(f) + scanActionsHtml(a.ticker, f) + `</div>`).join("");
      // (CARDS segue com os rótulos por célula — lá não há cabeçalho de coluna.)
      return `<li class="scan-row ${(a.melhor || {}).estado} ${(a.melhor || {}).direction || ""}">` +
        `<b class="scan-tk" data-open="${escapeHtml(a.ticker)}|${escapeHtml((a.melhor || {}).frame || "")}">${escapeHtml(a.ticker)}` +
        (scanCarimboDoAtivo(a) || "") + `</b>` +
        rows + `</li>`;
    }).join("") ||
      `<li class="scan-vazio">nada casa com <b>${escapeHtml(_scanBusca || "o filtro")}</b></li>`;
    scanLegenda(false);   // a legenda é do marcador da TABELA; em cards não há tabela
  }
  ligaAcoesDoScan(ul);
}

// Os cliques de "abrir a análise" — os mesmos nas três visões. Extraído porque a
// visão de SINAIS retorna antes do corpo da tabela e precisava do mesmo comportamento;
// uma segunda cópia divergiria no dia em que o atalho mudasse.
function ligaAcoesDoScan(ul) {
  ul.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", (ev) => {
    ev.stopPropagation();   // não dispara o data-open do frame-row pai
    const [tk, frame, method] = b.dataset.go.split("|");
    $("ticker").value = tk;
    if (frame) { _barTf = frame; }
    _barMethod = method;
    renderLaunchBar();
    $("scanPanel").classList.add("hidden");
    $("analyzeForm").requestSubmit();
  }));
  ul.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => {
    // Clicar o ticker (cabeça) ou qualquer frame-row abre a análise gráfica direto
    // (setup123 — leitura estrutural sem LLM, $0): preenche o ticker, seta o frame
    // da linha clicada e roda. Atalho mais rápido: ticker → gráfico 1-2-3.
    const [tk, frame] = b.dataset.open.split("|");
    $("ticker").value = tk;
    if (frame) { _barTf = frame; }
    _barMethod = "setup123";
    renderLaunchBar();
    $("scanPanel").classList.add("hidden");
    $("analyzeForm").requestSubmit();
  }));
}

// EXPECTATIVA, não só taxa de acerto. Acerto alto com alvo perto e stop longe é a
// armadilha clássica: com R:R 0,13 é preciso acertar 88,5% só pra EMPATAR. A linha
// diz quanto se ganha (ou perde) por trade em múltiplos de risco, o R:R médio que a
// sustenta, e o acerto de equilíbrio ao lado — sem isso a taxa sozinha engana.
function trackExpectancyHtml(data) {
  if (data.expectativa_r == null) {
    return data.n_fechados
      ? '<div class="scan-summary hint">expectativa indisponível — nenhum fechado com R:R conhecido</div>'
      : "";
  }
  const e = data.expectativa_r;
  const cls = e > 0 ? "ok" : "bad";
  const sinal = e > 0 ? "+" : "";
  const eq = data.acerto_equilibrio == null ? "—" : `${pctBR(data.acerto_equilibrio * 100)}%`;
  const p = data.acerto_com_rr == null ? "—" : `${Math.round(data.acerto_com_rr * 100)}%`;
  return `<div class="scan-summary">expectativa <b class="${cls}">${sinal}${e.toFixed(2)}R</b> por trade` +
    `<span class="hint"> — R:R médio ${data.rr_medio} · precisa de ${eq} pra empatar · base: ${data.n_com_rr} fechado(s) com R:R, ${p} de acerto neles</span></div>`;
}

async function showScanTrack() {
  const box = $("scanTrack");
  box.classList.toggle("hidden");
  if (box.classList.contains("hidden")) return;
  box.innerHTML = '<span class="hint">re-avaliando gatilhos…</span>';
  try {
    const res = await fetch("/api/scan/verdicts?date=" + encodeURIComponent($("date").value || ""));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "falha");
    const taxa = data.taxa_acerto == null ? "—" : `${Math.round(data.taxa_acerto * 100)}%`;
    const rows = (data.verdicts || []).slice().reverse().slice(0, 30).map((v) => {
      const vb = { bateu_tp: ["bateu TP", "ok"], bateu_sl: ["bateu SL", "bad"],
        andamento_lucro: ["no lucro", "ok"], andamento_prejuizo: ["no prejuízo", "bad"],
        // Série que não alcança o dia do gatilho: pode ter tocado sem ninguém ver.
        // É um estado próprio — chamar isso de "andamento" seria afirmar o que não
        // se sabe, e é assim que uma taxa de acerto vira ficção.
        sem_serie_cobrindo: ["sem série cobrindo", "warn"],
        sem_dado: ["— sem dado", ""] }[v.veredito] || [v.veredito, ""];
      return `<li class="scan-row"><span class="scan-line">` +
        `<b>${escapeHtml(v.ticker || "")}</b><span class="scan-frame">${escapeHtml(v.frame || "")}</span>` +
        `<span>${v.direction === "venda" ? "↓" : "↑"} gatilho ${scanFmt(v.trigger)}</span>` +
        `<span class="scan-dist">agora ${scanFmt(v.preco_agora)}</span>` +
        // Fechado carrega a DATA do toque: o veredito veio da série (a barra que
        // tocou o nível), não da comparação com o preço de hoje — por isso não
        // muda mais amanhã. Mostrar a data é o que torna isso verificável.
        (v.fechado && v.fechado_em
          ? `<span class="scan-dist">em ${escapeHtml(v.fechado_em)}${v.empate_na_barra ? " — TP e SL na mesma barra" : ""}</span>`
          : "") +
        // Alvo logado que não estava à frente da entrada (entradas gravadas antes do
        // fix): ignorado na leitura — o trade só pode fechar pelo SL. Dito na cara.
        (v.tp_ignorado ? `<span class="scan-dist">alvo inválido ignorado</span>` : "") +
        (v.motivo ? `<span class="scan-dist">${escapeHtml(v.motivo)}</span>` : "") +
        `<span class="scan-chip ${vb[1]}">${vb[0]}</span></span></li>`;
    }).join("");
    box.innerHTML = `<div class="scan-summary"><b>${taxa}</b> de acerto em ${data.n_fechados || 0} gatilho(s) fechado(s)` +
      `${data.taxa_acerto == null ? ' <span class="hint">(nenhum fechado ainda — os abertos aparecem abaixo)</span>' : ""}</div>` +
      trackExpectancyHtml(data) +
      (rows ? `<ul class="scan-list">${rows}</ul>` : '<span class="hint">nenhum gatilho flagrado ainda — escaneie que os em-gatilho passam a ser medidos</span>');
  } catch (e) { box.innerHTML = `<span class="error">${escapeHtml(e.message)}</span>`; }
}

function bindScan() {
  const open = $("scanOpenBtn");
  if (open) open.addEventListener("click", openScanPanel);
  const run = $("scanRunBtn");
  if (run) run.addEventListener("click", runScan);
  const track = $("scanTrackBtn");
  if (track) track.addEventListener("click", showScanTrack);
  const close = $("scanCloseBtn");
  if (close) close.addEventListener("click", () => $("scanPanel").classList.add("hidden"));
}


function init() {
  loadLlmCfg();
  applyConfig();
  bindConfig();
  $("analyzeForm").addEventListener("submit", startAnalysis);
  $("ticker").addEventListener("input", (e) => {
    const raw = $("ticker").value.trim();
    const t = raw.toUpperCase();
    $("assetHint").innerHTML = /-(USD|USDT)$|^BTC|^ETH/.test(t) ? `Detectado: cripto — inclui taxa de financiamento <span class="orig">(funding)</span>, contratos em aberto <span class="orig">(open interest)</span> e liquidações <span class="orig">(liquidations)</span>.` : "";
    // Seleção de sugestão (datalist) → fecha e NÃO reabre; digitar → busca normal.
    if (isTickerSuggestionPick(e, raw)) { closeTickerSuggest(); return; }
    scheduleTickerSuggest(raw);
  });
  $("netNote").textContent = "acesse por " + location.host;
  bindHistoryTabs();
  bindReeval();
  bindConfront();
  bindLaunchBar();
  bindScan();
  renderLaunchBar();   // barra ÚNICA de pé no boot (TFs + métodos) mesmo sem ativo aberto
  bindExportPdf();
  { const rb = $("resumeRunBtn"); if (rb) rb.addEventListener("click", resumeRun); }  // Retomar (task 026)
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
  bindHistoryScrollPrices();   // preço lazy: carrega ao rolar a lista (task 011)
  initColResizer();
}

// Divisória arrastável histórico ↔ conteúdo (task 018): arrasta pra redimensionar,
// cursor col-resize, largura persiste (localStorage), mín/máx sensatos pra o gráfico
// não esticar feio. Só no desktop (o resizer some ao empilhar; a var não afeta o 1fr).
const _SIDEBAR_KEY = "td_sidebar_w";
const _SIDEBAR_MIN = 200;
function _sidebarMax(layout) {
  const w = layout.getBoundingClientRect().width || 1200;
  return Math.max(_SIDEBAR_MIN + 40, Math.min(560, Math.round(w * 0.45)));
}
function _applySidebarWidth(layout, w) {
  const clamped = Math.max(_SIDEBAR_MIN, Math.min(_sidebarMax(layout), Math.round(w)));
  layout.style.setProperty("--sidebar-w", clamped + "px");
  return clamped;
}
function _redrawLiveCharts() {
  document.querySelectorAll("canvas").forEach((cv) => {
    if (cv._chart) drawPriceChart(cv, cv._chart, cv._actionable);
  });
}
function initColResizer() {
  const rz = document.getElementById("colResizer");
  const layout = document.querySelector("main.layout");
  if (!rz || !layout) return;
  const saved = parseInt(localStorage.getItem(_SIDEBAR_KEY) || "", 10);
  if (saved >= _SIDEBAR_MIN) _applySidebarWidth(layout, saved);
  let raf = null;
  const liveRedraw = () => { if (!raf) raf = requestAnimationFrame(() => { raf = null; _redrawLiveCharts(); }); };
  rz.addEventListener("pointerdown", (e) => {
    if (window.matchMedia("(max-width: 900px)").matches) return;   // empilhado: sem resize
    rz.setPointerCapture(e.pointerId);
    document.body.classList.add("col-resizing");
    e.preventDefault();
    const onMove = (ev) => {
      _applySidebarWidth(layout, ev.clientX - layout.getBoundingClientRect().left);
      liveRedraw();
    };
    const onUp = () => {
      rz.removeEventListener("pointermove", onMove);
      rz.removeEventListener("pointerup", onUp);
      document.body.classList.remove("col-resizing");
      const w = parseInt(getComputedStyle(layout).getPropertyValue("--sidebar-w"), 10);
      if (w) localStorage.setItem(_SIDEBAR_KEY, String(w));
      _redrawLiveCharts();
    };
    rz.addEventListener("pointermove", onMove);
    rz.addEventListener("pointerup", onUp);
  });
  // teclado (a11y): ← / → redimensionam em passos de 16px
  rz.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const cur = parseInt(getComputedStyle(layout).getPropertyValue("--sidebar-w"), 10) || 280;
    const w = _applySidebarWidth(layout, cur + (e.key === "ArrowRight" ? 16 : -16));
    localStorage.setItem(_SIDEBAR_KEY, String(w));
    _redrawLiveCharts();
    e.preventDefault();
  });
}

function onVisibleForeground() {
  if (document.visibilityState !== "visible") return;
  if (_watchedRunId && pollTimer) watchRun(_watchedRunId);   // poll já + intervalo novo
  else resumeActiveRun();                                    // timer morreu: reengata o vivo
  refreshPrices();                                           // preços live ao voltar pra aba
  revalidaSeOCandleFechouEnquantoEuNaoOlhava();              // candle que fechou atrás da aba
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
