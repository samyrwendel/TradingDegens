"""O 1-2-3 sozinho não acorda Padrão/Erick — e Padrão/Erick não perdem o 1-2-3.

Requisito do Samyr (28/08), em cima do fix do achatamento em ``runReanalyze()``:

1. **O atalho não escala.** ``method="setup123"`` sobe SÓ a run estrutural
   (``_start_estrutural``): zero analista selecionado, nenhum agente, $0 de LLM. O
   mesmo vale pro ``storm123``, que entrou pela mesma rota na task 022 — método
   NOVO, nunca uma flag deste.
2. **Os métodos completos não encolhem.** ``padrao`` e ``erick`` continuam
   escolhendo analistas e rodando o pipeline — o 1-2-3 segue DENTRO deles
   (o rótulo do botão Padrão é literalmente "MMS · 1-2-3"), nunca extraído
   pra fora.

A separação é de PIPELINE (qual run sobe), não de conteúdo. Os dois erros são
simétricos e os dois têm que ser impossíveis: o atalho virar análise completa
(o bug que existia) e a análise completa perder o 1-2-3 (o risco de "isolar" o
atalho sem cuidado).

Sem rede e sem LLM: os workers são substituídos por no-op, então o teste só
observa a DECISÃO DE ROTA que ``start()`` tomou.
"""

import pytest

from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore

TICKER = "AAPL"
DATE = "2026-08-25"


@pytest.fixture
def runner(tmp_path, monkeypatch):
    r = AnalysisRunner(
        base_config={
            "results_dir": str(tmp_path),
            "llm_provider": "openai",
            "deep_think_llm": "gpt-5.5",
            "quick_think_llm": "gpt-5.4-mini",
        },
        store=HistoryStore(tmp_path),
    )
    # Nenhum worker roda: nem o pipeline multi-agente, nem o do atalho. O que
    # importa aqui é a rota escolhida, não o resultado.
    monkeypatch.setattr(AnalysisRunner, "_worker", lambda self, run: None)
    monkeypatch.setattr(AnalysisRunner, "_worker_estrutural", lambda self, run: None)
    # Sem rede na classificação do ativo.
    monkeypatch.setattr(AnalysisRunner, "detect_asset_type", lambda self, t: "stock")
    return r


def _run_of(runner, run_id):
    run = runner._runs[run_id]
    assert run is not None, f"run {run_id} não registrada"
    return run


@pytest.mark.parametrize("metodo", ["setup123", "storm123"])
def test_metodo_estrutural_nao_seleciona_nenhum_analista(runner, metodo):
    """O atalho é estrutural: lista de analistas VAZIA — nenhum agente pra pagar.
    Vale pros DOIS: o Storm entrou como método próprio pela mesma rota (DA-078)."""
    run_id = runner.start(TICKER, DATE, method=metodo, reuse=False)
    run = _run_of(runner, run_id)
    assert run.method == metodo, ("método da run", run.method)
    assert run.selected_analysts == [], (
        f"o atalho {metodo} selecionou analista — isso é LLM cobrado num botão que "
        f"promete $0: {run.selected_analysts}"
    )


@pytest.mark.parametrize("method", ["padrao", "erick"])
def test_metodos_completos_continuam_com_analistas(runner, method):
    """Padrão e Erick seguem rodando o pipeline — o atalho não os esvaziou."""
    run_id = runner.start(TICKER, DATE, method=method, reuse=False)
    run = _run_of(runner, run_id)
    assert run.method != "setup123", (
        f"{method} foi desviado pra rota do atalho — perderia o pipeline inteiro"
    )
    assert run.selected_analysts, (
        f"{method} ficou sem analista nenhum: {run.selected_analysts}"
    )


def test_erick_traz_o_analista_erick_e_padrao_nao(runner):
    """A distinção entre os dois métodos completos continua de pé."""
    erick = _run_of(runner, runner.start(TICKER, DATE, method="erick", reuse=False))
    padrao = _run_of(runner, runner.start(TICKER, DATE, method="padrao", reuse=False))
    assert "erick" in erick.selected_analysts, erick.selected_analysts
    assert "erick" not in padrao.selected_analysts, padrao.selected_analysts


def test_rotas_sao_disjuntas(runner):
    """Cada método produz a SUA run. Nenhum completo cai na rota estrutural, e os
    dois estruturais não se confundem entre si — 1-2-3 e Storm são setups
    diferentes com a mesma numeração (DA-078), e a run tem que dizer qual é."""
    ids = {m: runner.start(TICKER, DATE, method=m, reuse=False)
           for m in ("setup123", "storm123", "padrao", "erick")}
    metodos = {m: _run_of(runner, rid).method for m, rid in ids.items()}
    assert metodos["setup123"] == "setup123", metodos
    assert metodos["storm123"] == "storm123", metodos
    assert metodos["padrao"] not in ("setup123", "storm123"), metodos
    assert metodos["erick"] not in ("setup123", "storm123"), metodos
