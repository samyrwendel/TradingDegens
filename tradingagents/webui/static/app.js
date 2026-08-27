"use strict";

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let TZ_LABEL = "GMT-4 (Manaus)";

// Segundo plano: cada análise roda numa thread própria no servidor e continua
// mesmo se o usuário troca de ativo, sai da tela ou recarrega. Estes controlam
// só a VISÃO — qual run está sendo acompanhado ao vivo, quais estavam rodando na
// última atualização da lista, e quais terminaram sozinhos (ganham "pronto").
let _watchedRunId = "";              // run cujo progresso está na tela agora
// Cancelamento PENDENTE (task 013): o cancel é cooperativo — a run só encerra no
// próximo limite de nó/LLM. Enquanto o pedido está pendente E a run segue 'running',
// o botão fica TRAVADO em 'parando…' e o poll NÃO o reabre (senão o usuário acha que
// o clique não pegou e clica de novo). Guarda o run_id pedido; some quando vira terminal.
let _cancelPending = "";
let _cancelPause = false;             // o pedido foi Pausar (true) ou Parar (false)?
const _STOP_LABEL = "⏹ Parar análise";
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
  // Cross-provider RÁPIDO/PESADO (task 027): no modo avançado, cada nível manda o
  // seu provedor; os modelos por nível reusam deep_think_llm/quick_think_llm acima.
  if (c.advanced) {
    body.advanced = true;
    if (c.quickProvider) body.quick_provider = c.quickProvider;
    if (c.deepProvider) body.deep_provider = c.deepProvider;
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
    stopBtn.textContent = _cancelPause ? "⏸ pausando…" : "⏹ parando…";
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
      headers: { ...headers, "Content-Type": "application/json" },
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
  if (!pendingHere) $("progressLabel").textContent = p.label || "";
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
      // rótulo do agente + selo de TIMEFRAME(s) da etapa (task 009) + selo do LLM que
      // rodou esta etapa (atribuição, task 024). Os dois selos vivem no mesmo lugar.
      sum.innerHTML = `<span class="tk-label">${escapeHtml(it.label)}</span>` +
        `<span class="tk-tf" data-tk-tf></span>` +
        `<span class="tk-model" data-tk-model></span>`;
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

// Selo de TIMEFRAME(s) da etapa (task 009): "⏱ semanal · diário" (Mercado) / "⏱ 4h · 15m"
// (Erick). Vazio nos nós que não operam num tempo gráfico (some via CSS :empty). O TF vem
// do backend (real do motor, não configurado) — nunca inventa aqui.
function stepTfLabel(it) {
  if (!it || !it.timeframe) return "";
  return "⏱ " + it.timeframe;
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
  return tf ? ` <span class="sec-tf">⏱ ${escapeHtml(tf)}</span>` : "";
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
        const tf = s.timeframe ? `<span class="as-tf">⏱ ${escapeHtml(s.timeframe)}</span>` : "";
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
    ? `<button type="button" class="err-action" data-act="open-config">⚙️ Abrir Configurações</button>`
    : `<span class="err-hint">Você pode tentar de novo pelos botões de método/timeframe acima.</span>`;
  return `<div class="error-card ${escapeHtml(code || "error")}">` +
    `<div class="err-title">⚠️ Não deu pra concluir</div>` +
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

// Linha de metadados (data · tipo · custo · tempo · conclusão) — mesma do sucesso.
function resultMetaHtml(snap) {
  const finished = snap.finished_at || (snap.result && snap.result.finished_at);
  return `<span>Data da análise <b>${escapeHtml(snap.date || "")}</b></span>` +
    `<span>Tipo <b>${escapeHtml(assetPt(snap.asset_type))}</b></span>` +
    `<span>Custo <b>${fmtCost(snap.cost)}</b></span>` +
    `<span>Tempo <b>${snap.elapsed || 0}s</b></span>` +
    (finished ? `<span>Interrompido <b>${fmtStamp(finished, true)}</b></span>` : "");
}

// Banner do erro PARCIAL: nomeia a etapa que falhou, diz que o resto está preservado
// abaixo, e traz a ação de continuar (abrir config quando é chave/crédito + escalar).
function partialBannerHtml(snap, r) {
  const step = (r.failed_step && r.failed_step.label) ? escapeHtml(r.failed_step.label) : "";
  const msg = snap.error || "Uma etapa falhou.";
  const wantsConfig = _CFG_ERROR_CODES.has(snap.error_code);
  const action = wantsConfig
    ? `<div class="err-foot"><button type="button" class="err-action" data-act="open-config">⚙️ Abrir Configurações</button></div>`
    : "";
  const stepLine = step
    ? `<div class="err-msg">Parou em: <b>${step}</b>. As etapas concluídas abaixo estão <b>preservadas</b> — continue do ponto (escale a etapa ou retome), sem refazer tudo.</div>`
    : `<div class="err-msg">As etapas concluídas abaixo estão <b>preservadas</b> — continue do ponto, sem refazer tudo.</div>`;
  return `<div class="error-card partial ${escapeHtml(snap.error_code || "error")}">` +
    `<div class="err-title">⚠️ Parou nesta etapa — o já feito foi preservado</div>` +
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
    html += `<details class="section erick" open><summary>🧭 Método Erick — recuo à média · saída · peso do trade${tfTag("erick")}${axisTag(axes.erick)}</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.erick_report)}</div></div></details>`;
  }
  html += section("⚖️ Juiz do Debate (Gestor de Pesquisa) — leitura", r.research_manager || r.investment_plan, axes.juiz);
  html += section("📊 Mercado — preço e múltiplos tempos gráficos", r.market_report, axes.tecnico, "market");
  html += section("📰 Notícias — macro e mercados de previsão", r.news_report);
  html += section("💬 Sentimento", r.sentiment_report);
  if (!isCrypto) html += section("📑 Fundamentos", r.fundamentals_report);
  html += section("🎯 Plano do Trader (leitura — insumo, não é o veredito)", r.trader_plan, axes.trader);
  html += section("🛡️ Decisão de Risco (parcial)", r.risk_decision);
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
    $("actionable").classList.add("hidden");
    $("headPrice").classList.add("hidden");
    $("verdictTf").classList.add("hidden");
    $("degradedBanner").classList.add("hidden");
    $("exportPdfBtn").classList.add("hidden");  // nada de análise pra exportar num run com erro
    $("confrontCtl").classList.add("hidden");   // não confrontar a partir de um run com erro
    // Reanálise segue disponível pela barra ÚNICA: uma falha (fonte fora do ar,
    // transitório) é justamente quando o usuário quer rerodar escolhendo método/TF.
    // Método aberto: preserva o que o run errado carregava (history traz r.method);
    // sem isso, cai em padrão (não inventa método num run que falhou).
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
    // + botão pra abrir ⚙️ Configurações quando é problema de chave/crédito.
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
  _openMethod = (r.erick_report && r.erick_report.trim()) ? "erick" : "padrao";
  _openView = _openMethod;   // a barra destaca o método aberto (Padrão/Erick)
  $("verdictBadge").className = verdictClass(r.verdict);
  $("verdictBadge").innerHTML = verdictHtml(r.verdict);
  renderVerdictCaveat(r.verdict_caveat, r.pre_judge_findings);
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
  syncLaunchBarToOpen();              // a barra passa a apontar pro aberto (método + frame do veredito)
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
  // Fallback transparente (task 027-fallback): se o motor trocou de provedor sozinho
  // em alguma etapa, o banner de resumo abre logo abaixo do QA — visível de relance.
  html += fallbackBannerHtml(r.fallbacks);
  if (r.erick_report && r.erick_report.trim()) {
    html += `<details class="section erick" open><summary>🧭 Método Erick — recuo à média · saída · peso do trade${tfTag("erick")}${axisTag(axes.erick)}</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.erick_report)}</div></div></details>`;
  }
  // For crypto, the deterministic derivatives feed goes first and open — it is
  // the data yfinance can't see and the source is always named here.
  if (isCrypto && r.derivatives_report && r.derivatives_report.trim()) {
    html += `<details class="section" open><summary>🪙 Derivativos — taxa de financiamento <span class="orig">(funding)</span> · contratos em aberto <span class="orig">(open interest)</span> · liquidações <span class="orig">(liquidations)</span> (fonte nomeada)</summary>` +
      `<div class="section-body"><div class="md">${renderMarkdown(r.derivatives_report)}</div></div></details>`;
  }
  html += section("⚖️ Juiz do Debate (Gestor de Pesquisa) — leitura", r.research_manager || r.investment_plan, axes.juiz);
  html += section("📊 Mercado — preço e múltiplos tempos gráficos", r.market_report, axes.tecnico, "market");
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
  _timeframes = ["1w", "1d", "4h", "1h", "15m"];
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
function normMethod(v) {
  return v === "compare" ? "compare" : (v === "erick" ? "erick" : "padrao");
}
function methodLabel(v) {
  return v === "compare" ? "Comparar" : (v === "erick" ? "Erick" : "Padrão");
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
  tfsEl.innerHTML = ALL_TFS.map(([tf, label]) => {
    const on = enabled.has(tf);
    const active = tf === _barTf;
    const cls = ["lb-tf", active ? "is-active" : "", on ? "" : "is-off"].filter(Boolean).join(" ");
    const title = on ? `Analisar no ${label}` : "Frame indisponível para este ativo (o backend não inventa candle)";
    return `<button type="button" class="${cls}" data-tf="${tf}" ${on ? "" : "disabled"} title="${escapeHtml(title)}">${escapeHtml(label)}</button>`;
  }).join("");
  const methods = [
    ["padrao", "Padrão", "Leitura Padrão (MMS · 1-2-3) no timeframe escolhido"],
    ["erick", "🧭 Erick", "Método Erick — recuo à média, saída antes da reversão, peso do trade"],
    ["compare", "⚖️ Comparar", "Roda as DUAS (Padrão e Erick) e confronta com o meta-juiz — a divergência é o sinal"],
  ];
  mEl.innerHTML = methods.map(([m, label, title]) => {
    const active = m === _barMethod;
    const cls = ["lb-method", m, active ? "is-active" : ""].filter(Boolean).join(" ");
    return `<button type="button" class="${cls}" data-method="${m}" aria-pressed="${active ? "true" : "false"}" title="${escapeHtml(title)}">${escapeHtml(label)}</button>`;
  }).join("");
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

// Provedor efetivo de um nível: no avançado (027) cada nível tem o seu; senão o simples.
function _effLevelProvider(level) {
  const c = _llmCfg || {};
  if (c.advanced) return (level === "deep" ? c.deepProvider : c.quickProvider) || c.provider ||
    (_llmMeta && _llmMeta.default_provider) || "";
  return c.provider || (_llmMeta && _llmMeta.default_provider) || "";
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
    const icon = level === "deep" ? "🧠" : "⚡";
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
    `<div class="lbp-head">${level === "deep" ? "🧠" : "⚡"} Modelo ${lead}` +
    (prov ? ` <span class="lbp-prov">${escapeHtml(prov)}</span>` : "") + `</div>` +
    `<input type="text" class="lbp-search" autocomplete="off" role="combobox" aria-autocomplete="list" ` +
      `placeholder="filtrar modelos… (id ou nome)" />` +
    `<ul class="lbp-list" role="listbox"></ul>` +
    `<div class="lbp-foot">` +
      `<button type="button" class="lbp-default">padrão do provedor</button>` +
      `<button type="button" class="lbp-adv" title="Provedor por nível (Rápido × Pesado, cross-provider)">⚙️ avançado</button>` +
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
  el.querySelector(".lbp-adv").addEventListener("click", () => { _closeLaunchModelPop(); openConfigAdvanced(); });
  document.addEventListener("mousedown", _lbPopOutside, true);
  document.addEventListener("keydown", _lbPopEsc, true);
  setTimeout(() => input.focus(), 0);
}

// Abre o config já no Avançado (cross-provider por nível, task 027) e rola até ele —
// mantém o avançado "junto" do seletor sem duplicá-lo no launcher.
function openConfigAdvanced() {
  const panel = $("configPanel");
  if (panel) panel.classList.remove("hidden");
  const adv = $("cfgAdvanced");
  if (adv && !adv.checked) { adv.checked = true; applyAdvancedVisibility(); }
  if (!_modelItems.length) refreshModels();
  const grid = $("cfgAdvancedGrid") || panel;
  if (grid && grid.scrollIntoView) grid.scrollIntoView({ behavior: "smooth", block: "center" });
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
    notes.push(`${demo} Padrão 1-2-3 ${dlabel}: gatilho ${verb} ${fmtNum(pat.trigger)} — <b>${PAT_STATE[pat.state] || pat.state}</b>.`);
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
  // Barra ÚNICA (task 029): Analisar roda com o método + timeframe escolhidos na barra.
  // Comparar dispara as DUAS (Padrão × Erick, compare=true); Erick/Padrão vão no method.
  const compare = _barMethod === "compare";
  const method = _barMethod === "erick" ? "erick" : "padrao";
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
    // contagem de análises do ticker: vem do backend (watchlist varre o index inteiro,
    // task 011); ``n`` (ocorrências na lista) é fallback pra payloads antigos.
    const cnt = r.count || n;
    const badge = cnt > 1 ? `<span class="h-count" title="${cnt} análises">${cnt}</span>` : "";
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
        `<span class="h-verdict ${vClass}" title="${escapeHtml(running ? "em andamento" : v)}">${vHtml}</span>` +
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

// Preenche o <select> de provedores e reflete a config salva nos campos.
function renderConfigPanel() {
  const sel = $("cfgProvider");
  if (!sel || !_llmMeta) return;
  // Provedores owner-only (assinatura do dono, ex.: claude-cli · $0/token) só
  // aparecem pro dono logado — o público nem os vê (o server barra em profundidade).
  const list = (_llmMeta.providers || []).filter((p) => _isOwner || !p.owner_only);
  let cur = _llmCfg.provider || _llmMeta.default_provider || "openai";
  // provedor salvo é owner-only mas a sessão não é dona: cai no default visível
  if (!list.some((p) => p.id === cur)) cur = _llmMeta.default_provider || "openai";
  sel.innerHTML = list.map((p) =>
    `<option value="${escapeHtml(p.id)}"${p.id === cur ? " selected" : ""}>${escapeHtml(p.label)}</option>`
  ).join("");
  $("cfgKey").value = _llmCfg.apiKey || "";
  $("cfgQuick").value = _llmCfg.quickModel || "";
  $("cfgDeep").value = _llmCfg.deepModel || "";
  $("cfgBaseUrl").value = _llmCfg.baseUrl || "";
  syncProviderFields(cur);
  renderLevelProviders(list, cur);
  // Config salva pode trazer o modelo no formato do provedor ANTERIOR (o bug: trocou
  // pra assinatura Claude e ficou "anthropic/claude-opus-5" do OpenRouter → 404).
  // Normaliza ANTES de montar os combos, com os selects de nível já preenchidos.
  // Não-estrito: aqui provedor e modelo foram salvos JUNTOS — corrigir o formato é
  // seguro, trocar um id fora do catálogo (fine-tune próprio) pelo default não é.
  normalizeConfigModels({ strict: false });   // task 016
  applyModelCombosForProviders();   // combos refletem o provedor de cada nível (task 014)
  renderOwnerBox();
  renderSubscriptionBox();
  updateConfigBadge();
  renderLaunchModels();   // reflete os modelos salvos nos chips do launcher (task 012)
}

// Cross-provider RÁPIDO/PESADO (task 027): popula os selects de provedor por nível
// (Rápido/Pesado) com a MESMA lista visível do provedor simples, e reflete o estado
// avançado salvo. Cada nível default = o provedor simples atual.
function renderLevelProviders(list, cur) {
  const qs = $("cfgQuickProvider");
  const ds = $("cfgDeepProvider");
  const adv = $("cfgAdvanced");
  if (!qs || !ds || !adv) return;
  const opts = (selected) => list.map((p) =>
    `<option value="${escapeHtml(p.id)}"${p.id === selected ? " selected" : ""}>${escapeHtml(p.label)}</option>`
  ).join("");
  let q = _llmCfg.quickProvider || cur;
  let d = _llmCfg.deepProvider || cur;
  if (!list.some((p) => p.id === q)) q = cur;
  if (!list.some((p) => p.id === d)) d = cur;
  qs.innerHTML = opts(q);
  ds.innerHTML = opts(d);
  adv.checked = !!_llmCfg.advanced;
  applyAdvancedVisibility();
}

// Mostra/esconde a grade avançada conforme o toggle e REALOCA os campos de modelo
// (task 016): no avançado, o modelo de cada nível fica AO LADO do provedor daquele
// nível (Provedor do Rápido → Modelo do Rápido, idem Pesado). Antes os dois campos
// ficavam lá em cima, junto do provedor simples, parecendo um modelo COMPARTILHADO —
// era o convite pro mismatch de formato. São os MESMOS inputs (mesmo id, mesmo combo,
// mesmo estado): só mudam de lugar, sem duplicar fonte de verdade.
function applyAdvancedVisibility() {
  const on = !!($("cfgAdvanced") && $("cfgAdvanced").checked);
  ["cfgAdvancedGrid", "cfgAdvancedNote"].forEach((id) => {
    const el = $(id);
    if (el) el.classList.toggle("hidden", !on);
  });
  const grid = $("cfgAdvancedGrid");
  const byok = $("byokGrid");
  const qf = $("cfgQuickField");
  const df = $("cfgDeepField");
  const dpf = $("cfgDeepProviderField");
  if (grid && byok && qf && df && dpf) {
    if (on) {
      grid.insertBefore(qf, dpf);   // …Provedor do Rápido · Modelo do Rápido…
      grid.appendChild(df);         // …Provedor do Pesado · Modelo do Pesado
    } else if (qf.parentNode !== byok) {
      byok.appendChild(qf);         // volta pro fim da grade simples (ordem original)
      byok.appendChild(df);
    }
  }
  syncLevelModelLabels();
}

// Rótulo dos campos de modelo: no avançado diz de QUEM é o modelo e em que provedor
// ele roda ("Modelo do Rápido (claude-cli)") — o formato do id é o daquele provedor.
function syncLevelModelLabels() {
  const on = !!($("cfgAdvanced") && $("cfgAdvanced").checked);
  [["quick", "cfgQuickLabel", "cfgQuick", "Rápido", "Modelo rápido"],
   ["deep", "cfgDeepLabel", "cfgDeep", "Pesado", "Modelo pesado"]].forEach(
    ([lvl, lid, fid, lead, plain]) => {
      const el = $(lid);
      if (!el) return;
      const orig = on ? (_cfgLevelProvider(lvl) || lvl) : lvl;
      el.innerHTML = `${escapeHtml(on ? "Modelo do " + lead : plain)} `
        + `<span class="orig">(${escapeHtml(orig)})</span>`;
      el.setAttribute("for", fid);
    });
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
// "✅ Label conectada · Desconectar"). O JS alterna as duas conforme o status.
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
      ? `✅ ${meta.label} conectada · login do servidor`
      : `✅ ${meta.label} conectada`;
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
    st.textContent = "⚠️ Sua sessão expirou (o servidor reiniciou). Entre de novo pra conectar assinaturas.";
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
      if (st) { st.textContent = "❌ " + (data.error || "não deu pra iniciar"); st.className = "cfg-status sub-status err"; }
      return;
    }
    // Abre o login oficial numa nova aba; o usuário autoriza lá e volta.
    window.open(data.authorize_url, "_blank", "noopener");
    if (st) { st.textContent = `aguardando você autorizar no ${meta.label}…`; st.className = "cfg-status sub-status"; }
    startSubscriptionPoll();
  } catch (e) {
    if (st) { st.textContent = "❌ erro de rede"; st.className = "cfg-status sub-status err"; }
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
      st.textContent = "✅ assinatura conectada"; st.className = "cfg-status sub-status ok";
    } else if (handleOwnerSessionLost(res, data)) {
      return;                                 // sessão caiu no restart: já pedimos re-login
    } else {
      st.textContent = "❌ " + (data.error || "falhou"); st.className = "cfg-status sub-status err";
    }
  } catch (e) {
    st.textContent = "❌ erro de rede"; st.className = "cfg-status sub-status err";
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
  const advanced = !!($("cfgAdvanced") && $("cfgAdvanced").checked);
  const quickProv = (advanced && $("cfgQuickProvider") && $("cfgQuickProvider").value) || provId;
  const deepProv = (advanced && $("cfgDeepProvider") && $("cfgDeepProvider").value) || provId;
  return {
    provider: provId,
    apiKey: $("cfgKey").value.trim(),
    // Modelo SEMPRE no FORMATO do provedor do seu nível (task 016): é este objeto que
    // vira _llmCfg no Salvar e alimenta o corpo do analyze/Testar — normalizar aqui
    // impede que um id colado no formato de outro provedor chegue à API. Não-estrito:
    // o que o usuário DIGITOU é escolha dele (fine-tune, deploy próprio); quem reseta
    // sobra de outro provedor é a troca de provedor, não a leitura do formulário.
    quickModel: normalizeModelForProvider(quickProv, $("cfgQuick").value.trim(), "quick", { strict: false }),
    deepModel: normalizeModelForProvider(deepProv, $("cfgDeep").value.trim(), "deep", { strict: false }),
    baseUrl: (p && p.needs_base_url) ? $("cfgBaseUrl").value.trim() : "",
    // Cross-provider RÁPIDO/PESADO (task 027): provedor por nível no modo avançado.
    advanced,
    quickProvider: advanced && $("cfgQuickProvider") ? $("cfgQuickProvider").value : "",
    deepProvider: advanced && $("cfgDeepProvider") ? $("cfgDeepProvider").value : "",
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

// Troca do provedor SIMPLES/base (task 014): sincroniza os modelos pro provedor. Fora
// do avançado, reseta Rápido/Pesado pros defaults do provedor (mata o mismatch). No
// avançado os modelos seguem os provedores por-nível — não mexe neles aqui.
function onSimpleProviderChange(prov) {
  setCfgStatus("");
  syncProviderFields(prov);
  const p = _providerMeta(prov);
  const advanced = $("cfgAdvanced") && $("cfgAdvanced").checked;
  if (!advanced) {
    $("cfgQuick").value = (p && p.default_quick) || "";
    $("cfgDeep").value = (p && p.default_deep) || "";
  } else {
    // no avançado os modelos seguem os provedores POR NÍVEL — mas um nível que caia
    // no provedor-base (select vazio) precisa do id no formato NOVO (task 016).
    normalizeConfigModels();
  }
  applyModelCombosForProviders();             // catálogo já reflete o novo provedor
  const suggest = _isOwner && !!_subConnected.anthropic && prov === "anthropic";
  const form = _readConfigForm();
  refreshModelsForProvider(prov, { apiKey: form.apiKey, baseUrl: form.baseUrl, status: !suggest });
  if (suggest) maybeSuggestClaudeCli();
  renderLaunchModels();
}

// Troca do provedor de UM nível no avançado (task 014): o modelo daquele nível vai pro
// FORMATO do novo provedor e o combo lista os modelos dele (ao vivo se der, senão
// catálogo). Nunca deixa o nível com um modelo de outro provedor. Um id compatível
// (mesma família, ex.: claude-sonnet-5 do Anthropic pago → assinatura) é PRESERVADO;
// só o incompatível cai no default (task 016) — antes qualquer troca clobberava.
function onLevelProviderChange(level) {
  const sel = level === "deep" ? $("cfgDeepProvider") : $("cfgQuickProvider");
  const fid = level === "deep" ? "cfgDeep" : "cfgQuick";
  const dk = level === "deep" ? "default_deep" : "default_quick";
  const prov = sel ? sel.value : "";
  const p = _providerMeta(prov);
  if ($(fid)) {
    const cur = $(fid).value.trim();
    $(fid).value = cur ? normalizeModelForProvider(prov, cur, level) : ((p && p[dk]) || "");
  }
  syncLevelModelLabels();
  applyModelCombosForProviders();
  const form = _readConfigForm();
  // a chave BYOK é do provedor-base; um nível diferente lista pela env do dono (ou catálogo).
  refreshModelsForProvider(prov, { apiKey: prov === form.provider ? form.apiKey : "", baseUrl: form.baseUrl });
  maybeSuggestClaudeCli();
  renderLaunchModels();
}

// Dono com assinatura Claude conectada mas usando o Anthropic PAGO (simples ou por-nível):
// sugere a assinatura ($0/token) — a escolha óbvia pra Claude sem gastar chave (task 014).
function maybeSuggestClaudeCli() {
  if (!_isOwner || !_subConnected.anthropic) return;
  if (_cfgLevelProvider("quick") === "anthropic" || _cfgLevelProvider("deep") === "anthropic") {
    setCfgStatus("💡 Assinatura Claude conectada — escolha “Claude — assinatura ($0/token)” em vez do Anthropic pago pra rodar sem gastar chave.", "");
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
  // Trocar de provedor SINCRONIZA os modelos pro provedor escolhido (task 014) — nunca
  // deixa Anthropic com modelo OpenAI. O catálogo reflete na hora; a lista ao vivo enriquece.
  $("cfgProvider").addEventListener("change", (e) => onSimpleProviderChange(e.target.value));
  // Cross-provider (task 027/014): provedor POR-NÍVEL — cada troca ressincroniza o modelo
  // daquele nível pros modelos do seu provedor (Rápido↔quickProvider, Pesado↔deepProvider).
  const qp = $("cfgQuickProvider"); if (qp) qp.addEventListener("change", () => onLevelProviderChange("quick"));
  const dp = $("cfgDeepProvider"); if (dp) dp.addEventListener("change", () => onLevelProviderChange("deep"));
  // O toggle Avançado mostra os provedores por nível e ressincroniza os combos.
  const adv = $("cfgAdvanced");
  if (adv) adv.addEventListener("change", () => {
    applyAdvancedVisibility(); setCfgStatus("");
    applyModelCombosForProviders(); preselectDefaults(); maybeSuggestClaudeCli();
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

// O provedor SALVO sumiu da lista? (provedor owner-only com a sessão deslogada: o
// select cai no default visível). Aí a tela NÃO representa a escolha do usuário e
// normalizar contra o provedor visível apagaria os modelos dele — melhor não tocar.
function _savedProviderHidden() {
  const saved = (_llmCfg && _llmCfg.provider) || "";
  const sel = $("cfgProvider");
  if (!saved || !sel) return false;
  return !Array.from(sel.options).some((o) => o.value === saved);
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

// Provedor de um nível NO CONFIG (lê os selects): avançado → por-nível; senão o simples.
function _cfgLevelProvider(level) {
  const adv = $("cfgAdvanced") && $("cfgAdvanced").checked;
  if (adv) {
    const sel = level === "deep" ? $("cfgDeepProvider") : $("cfgQuickProvider");
    if (sel && sel.value) return sel.value;
  }
  return ($("cfgProvider") && $("cfgProvider").value) || "";
}

// Realimenta cada combo do config com os modelos do SEU provedor de nível (task 014):
// no avançado o Rápido lista o provedor do Rápido e o Pesado o do Pesado; no simples
// os dois listam o provedor único.
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
// por provedor. Cancela a requisição anterior. Sucesso ✅ popula; provedor sem listagem
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
      if (status) setCfgStatus(`✅ chave válida — ${data.count} modelos carregados`, "ok");
    } else {
      applyModelCombosForProviders();             // catálogo do provedor
      if (status) setCfgStatus(`❌ ${data.error || "não deu pra listar os modelos"}`, "err");
    }
  } catch (e) {
    if (e.name === "AbortError" || seq !== _modelsSeq) return;
    if (status) setCfgStatus("❌ erro de rede ao listar modelos", "err");
  }
}

// Entry-point do provedor SIMPLES/base (mesma assinatura de antes — mantém os callers).
async function refreshModels() {
  const form = _readConfigForm();
  return refreshModelsForProvider(form.provider, {
    apiKey: form.apiKey, baseUrl: form.baseUrl, status: true,
  });
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
