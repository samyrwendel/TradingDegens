"""Contrato das entradas de ``degraded_sources`` até a UI (task 20260828-003).

O bug: o banner do webui mostrou literalmente "Análise feita SEM a fonte: **fonte**",
sem nome e sem motivo, num run do AAOI. Causa raiz — o guard de sanidade do debate
empurrava uma STRING solta no canal ``degraded_sources``, cujo contrato (fixado pelo
``make_resilient_analyst`` e lido pelo front) é o dict ``{label, report_key, reason,
kind}``. ``d.label``/``d.reason`` viravam ``undefined`` no JS: placeholder + ``<ul>``
vazio.

Estes testes travam as três camadas:
  1. os DOIS produtores emitem o dict completo;
  2. a fronteira da webui normaliza registro/checkpoint gravado antes do fix;
  3. o caminho de leitura de disco (que alimenta UI, confronto E reuso) normaliza.
"""

from __future__ import annotations

import pytest

from tradingagents.agents.utils.debate_utils import degraded_entry
from tradingagents.agents.utils.text_sanity import sanity_report
from tradingagents.graph.resilience import make_resilient_analyst
from tradingagents.webui.degraded import normalize_degraded, normalize_result

CORRUPT = "o preço es'tá caindo e o bear fica dezANIMAdO com d%d% de chance"

LEGACY_NOTE = (
    "Bear Researcher (texto suspeito: severity=suspect invented=20 (1.73%) "
    "[DILUÇÃO, dilução, DILUÇÃO, Dilução])"
)

REQUIRED_KEYS = {"label", "report_key", "reason", "kind"}


# --------------------------------------------------------------------------
# 1. Os dois produtores falam a MESMA língua
# --------------------------------------------------------------------------

def test_both_producers_emit_the_same_complete_shape():
    def boom(_state):
        raise RuntimeError("boom")

    from_analyst = make_resilient_analyst(boom, "news_report", "News Analyst")(
        {"messages": []}
    )["degraded_sources"][0]
    from_debate = degraded_entry(
        "Bear Researcher", sanity_report(CORRUPT), report_key="investment_debate_state"
    )

    assert set(from_analyst) >= REQUIRED_KEYS
    assert set(from_debate) >= REQUIRED_KEYS
    # e nenhum dos dois deixa o nome vazio — é o nome que o banner exibe
    assert from_analyst["label"] == "News Analyst"
    assert from_debate["label"] == "Bear Researcher"


def test_producers_disagree_on_kind_because_they_mean_different_things():
    """Fonte ausente ≠ texto sinalizado. O banner escreve frases opostas."""
    def boom(_state):
        raise RuntimeError("boom")

    absent = make_resilient_analyst(boom, "news_report", "News Analyst")(
        {"messages": []}
    )["degraded_sources"][0]
    flagged = degraded_entry(
        "Bear Researcher", sanity_report(CORRUPT), report_key="investment_debate_state"
    )
    assert absent["kind"] == "missing"
    assert flagged["kind"] == "suspect"


# --------------------------------------------------------------------------
# 2. Normalização da fronteira
# --------------------------------------------------------------------------

def test_legacy_string_note_recovers_label_and_reason():
    (entry,) = normalize_degraded([LEGACY_NOTE])
    assert entry["label"] == "Bear Researcher"       # nunca mais o placeholder
    assert "invented=20" in entry["reason"]          # e o motivo reaparece na lista
    assert entry["kind"] == "suspect"


def test_legacy_regenerated_note_also_parses():
    note = "Neutral Analyst (regenerado/degradado: severity=degraded structural=1)"
    (entry,) = normalize_degraded([note])
    assert entry["label"] == "Neutral Analyst"
    assert entry["reason"].startswith("regenerado/degradado")


def test_structured_entry_passes_through_untouched():
    src = {"label": "News Analyst", "report_key": "news_report",
           "reason": "RuntimeError: down", "kind": "missing"}
    assert normalize_degraded([src]) == [src]


def test_dict_without_kind_defaults_to_missing():
    """Entradas gravadas antes do campo ``kind`` só podiam vir do resilience."""
    (entry,) = normalize_degraded(
        [{"label": "News Analyst", "report_key": "news_report", "reason": "x"}]
    )
    assert entry["kind"] == "missing"


def test_unknown_kind_falls_back_to_missing():
    (entry,) = normalize_degraded([{"label": "X", "kind": "bogus"}])
    assert entry["kind"] == "missing"


def test_every_entry_always_has_all_keys_as_strings():
    for entry in normalize_degraded([LEGACY_NOTE, {"label": "X"}, {}, 42]):
        assert set(entry) == REQUIRED_KEYS
        assert all(isinstance(v, str) for v in entry.values())


def test_unparseable_text_keeps_the_reason_and_leaves_the_placeholder_as_last_resort():
    (entry,) = normalize_degraded(["fonte caiu e ninguém anotou quem"])
    assert entry["label"] == ""      # o front cai em "fonte" — só AQUI
    assert entry["reason"] == "fonte caiu e ninguém anotou quem"


@pytest.mark.parametrize("junk", [None, "", "   ", 0])
def test_empty_entries_are_dropped(junk):
    assert normalize_degraded([junk]) == []


@pytest.mark.parametrize("junk", [None, "", {}, 42, "abc"])
def test_non_list_input_is_empty(junk):
    assert normalize_degraded(junk) == []


def test_normalize_result_is_a_noop_without_degraded():
    for res in (None, {}, {"degraded": []}, "nope"):
        assert normalize_result(res) is res


def test_normalize_result_rewrites_in_place():
    res = {"verdict": "Sell", "degraded": [LEGACY_NOTE]}
    assert normalize_result(res) is res
    assert res["degraded"][0]["label"] == "Bear Researcher"


# --------------------------------------------------------------------------
# 3. Caminho de leitura de disco — o registro do AAOI já gravado
# --------------------------------------------------------------------------

def test_stored_record_is_normalized_on_read(tmp_path):
    """O run já em disco (o do print do Samyr) reabre com a fonte NOMEADA.

    Também cobre o reuso: ``_record`` é a única porta pro ``HistoryStore.get``, e
    o reuso copia esse ``result`` pra frente — sem isso a string voltaria a
    aparecer numa run NOVA.
    """
    from tradingagents.webui.runner import AnalysisRunner

    runner = AnalysisRunner.__new__(AnalysisRunner)          # sem side effects de __init__
    from tradingagents.webui.store import HistoryStore
    runner.store = HistoryStore(tmp_path)
    runner.store.save({
        "run_id": "20260827-213215-6ebd31",
        "ticker": "AAOI", "status": "done",
        "result": {"verdict": "Sell", "degraded": [LEGACY_NOTE]},
    })

    rec = runner._record("20260827-213215-6ebd31")
    (entry,) = rec["result"]["degraded"]
    assert entry["label"] == "Bear Researcher"
    assert entry["reason"]
    assert entry["kind"] == "suspect"


def test_record_returns_none_for_unknown_run(tmp_path):
    from tradingagents.webui.runner import AnalysisRunner
    from tradingagents.webui.store import HistoryStore

    runner = AnalysisRunner.__new__(AnalysisRunner)
    runner.store = HistoryStore(tmp_path)
    assert runner._record("nope") is None
