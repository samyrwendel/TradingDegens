""""Setup123" e "Storm123" são RÓTULO, não identificador (task 20260830-005).

Pedido do Samyr: "coloca Setup123 e Storm123 pra eu identificar". A razão é boa — os
dois métodos SÃO um 1-2-3, e a diferença está em QUAL; "1-2-3" × "Storm" obrigava a
lembrar de qual era qual.

O RISCO da mudança é o inverso do pedido: trocar o VALOR interno (`setup123` /
`storm123`) quebraria histórico salvo, reúso de registro e o track record — e a
restrição do Samyr é explícita ("sem desfazer o Setup123"). Estes testes separam as
duas coisas:

  * o rótulo de tela mudou, em toda superfície onde o método aparece;
  * o identificador NÃO mudou, e uma run gravada ANTES continua sendo lida, reusada e
    contabilizada igual;
  * ninguém compara método por TEXTO DE TELA — se comparasse, renomear o rótulo teria
    mudado comportamento em silêncio, que é o pior desfecho possível aqui.
"""

import json
import pathlib
import re
import threading

import pytest

from tradingagents.webui.compare import detect_method
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.scanner import ScanLog, _setup_da_entrada
from tradingagents.webui.store import HistoryStore

_STATIC = (pathlib.Path(__file__).resolve().parents[1]
           / "tradingagents" / "webui" / "static")


# ─────────────────────────── o identificador NÃO mudou ───────────────────────
def test_o_valor_interno_continua_setup123_e_storm123():
    """O que viaja na API, no store e no ledger. Renomear isto seria desfazer o
    Setup123 — exatamente o que foi proibido."""
    js = (_STATIC / "app.js").read_text()
    assert 'new Set(["padrao", "erick", "setup123", "storm123", "compare"])' in js
    assert 'new Set(["setup123", "storm123"])' in js


def test_uma_run_gravada_ANTES_continua_sendo_lida_e_classificada(tmp_path):
    """Registro no formato antigo (só o marcador `setup123: true`, sem nada do que
    veio depois): continua sendo reconhecido como leitura estrutural do Setup123."""
    antiga = {"result": {"setup123": True, "erick_report": "", "verdict": None}}
    assert detect_method(antiga) == "setup123"
    nova_storm = {"result": {"storm123": True, "setup123": False}}
    assert detect_method(nova_storm) == "storm123"
    # e uma run de MÉTODO continua sendo método (o rótulo não contaminou a detecção)
    assert detect_method({"result": {"erick_report": "x"}}) == "erick"
    assert detect_method({"result": {}}) == "padrao"


def test_o_ledger_antigo_continua_contando_no_track_record(tmp_path):
    """A linha gravada antes de o campo `setup` existir é do Setup123 e continua
    entrando na conta — o rótulo de tela não toca em nada disto."""
    p = tmp_path / "scans.jsonl"
    p.write_text(json.dumps({"ts": "2026-08-01T10:00:00+00:00", "ticker": "AAA",
                             "frame": "1d", "trigger": 10.0, "sl": 9.0,
                             "tp": 12.0, "rr": 2.0}) + "\n")
    e = ScanLog(p).entries()
    assert len(e) == 1
    assert _setup_da_entrada(e[0]) == "123"


def test_o_reuso_de_run_continua_keyado_pelo_metodo(tmp_path, monkeypatch):
    """DA-058: uma run idêntica volta inteira. A chave é o MÉTODO — se o rótulo
    tivesse virado identificador, o reúso pararia de casar com o que está em disco."""
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    monkeypatch.setattr(AnalysisRunner, "_worker_estrutural", lambda self, run: None)
    monkeypatch.setattr(AnalysisRunner, "detect_asset_type", lambda self, t: "stock")
    rid = runner.start("AAPL", "2026-08-25", method="setup123", reuse=False)
    assert runner._runs[rid].method == "setup123"
    rid2 = runner.start("AAPL", "2026-08-25", method="storm123", reuse=False)
    assert runner._runs[rid2].method == "storm123"


def test_ninguem_compara_metodo_por_TEXTO_DE_TELA():
    """Se algum lugar comparasse `=== "1-2-3"` ou `=== "Storm"`, esta renomeação teria
    mudado comportamento em silêncio. A varredura procura exatamente esse padrão."""
    js = (_STATIC / "app.js").read_text()
    suspeitos = re.findall(r'===\s*"(1-2-3|Storm|Setup123|Storm123|Padrão|Erick)"', js)
    assert suspeitos == [], ("método comparado por rótulo de tela", suspeitos)


# ───────────────────────────── o rótulo mudou na tela ────────────────────────
pytestmark_integration = pytest.mark.integration

try:
    from playwright.sync_api import sync_playwright as _spw
    sync_playwright = _spw
except Exception:  # noqa: BLE001
    sync_playwright = None


@pytest.fixture
def base(tmp_path):
    from tradingagents.webui.server import make_server

    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_os_chips_da_barra_dizem_Setup123_e_Storm123(base):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        page.wait_for_selector("#launchMethods .lb-method")
        m = page.evaluate("""() => [...document.querySelectorAll('#launchMethods .lb-method')]
            .map(b => ({valor: b.dataset.method, rotulo: b.innerText.trim()}))""")
        por_valor = {x["valor"]: x["rotulo"] for x in m}
        # o VALOR (o que vai pra API) intacto; o RÓTULO (o que ele lê) novo
        assert por_valor["setup123"] == "Setup123", m
        assert por_valor["storm123"] == "Storm123", m
        assert set(por_valor) == {"padrao", "erick", "setup123", "storm123", "compare"}, m
        browser.close()


@pytest.mark.integration
@pytest.mark.skipif(sync_playwright is None, reason="Playwright/Chromium ausente")
def test_o_rotulo_sai_do_VALOR_e_nao_de_texto_solto(base):
    """A tradução mora num lugar só (`methodLabel`). Um rótulo escrito à mão em cada
    superfície é como se perde a consistência na terceira tela."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.goto(base, wait_until="networkidle")
        m = page.evaluate("""() => ({
          setup: methodLabel('setup123'), storm: methodLabel('storm123'),
          padrao: methodLabel('padrao'), erick: methodLabel('erick'),
          desconhecido: methodLabel('inventado'),
        })""")
        assert m["setup"] == "Setup123" and m["storm"] == "Storm123", m
        assert m["padrao"] == "Padrão" and m["erick"] == "Erick", m
        assert m["desconhecido"] == "Padrão", ("método desconhecido não inventa nome", m)
        browser.close()
