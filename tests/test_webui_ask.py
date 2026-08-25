"""Q&A ancorado sobre uma análise já computada (task 027, endpoint /api/ask).

O valor do recurso está no GROUNDING: a resposta cita NÍVEIS reais da run (EMA
8/21/50, MMS, zonas, gatilho 1-2-3) e nunca inventa quando não há base. Estes
testes travam as funções puras que montam esse contexto — o número certo entra,
a média ausente vira "sem dado" (não pega o número vizinho), e uma run sem níveis
não finge ter. ``AnalysisRunner.ask`` é exercido com um LLM falso (sem rede) só
pra garantir o encanamento (custo medido, run desconhecida → None).
"""

import pytest

from tradingagents.webui import ask as A
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore

# --- amostras: o shape real que o runner persiste ---------------------------

def _single_record():
    return {
        "run_id": "r-single",
        "ticker": "SPCX",
        "verdict": "Underweight",
        "verdict_timeframe": "1d",
        "result": {
            "verdict": "Underweight",
            "timeframe": "1d",
            "erick_report": "Aguardar o recuo à média antes de entrar com peso.",
            "trader_plan": "Plano: manter, aguardar pullback.",
            "actionable": {
                "as_of": "2026-08-24",
                "price": 135.0,
                "buy_zone": {"label": "MMS20 — preço na média agora",
                             "price": 129.02, "low": 123.87, "high": 134.17},
                "realize_zone": {"label": "topo anterior 2026-08-17",
                                 "price": 149.8, "low": 144.65, "high": 154.95},
                "pullback_zone": None,
                "pattern": {"trigger": 104.83, "state": "formando", "direction": "venda"},
            },
            "price_chart": {
                # MMS 200 ausente de propósito (sem histórico) -> "sem dado".
                "ema": {"8": [130.0, 136.93], "21": [131.0, 134.07], "50": [140.0, 138.79]},
                "ma": {"20": [128.0, 129.02], "50": [142.0, 141.5], "200": [None, None]},
            },
        },
    }


def _compare_record():
    return {
        "run_id": "r-cmp",
        "ticker": "MSFT",
        "verdict": "Buy",
        "verdict_timeframe": "1d",
        "result": {
            "verdict": "Buy",
            "compare": {
                "a": {
                    "method": "padrao", "label": "Padrão · diário", "verdict": "Buy",
                    "trader_plan": "Padrão: comprar no rompimento.",
                    "actionable": {"as_of": "2026-08-21", "price": 483.24},
                    "price_chart": {"ema": {"8": [484.17], "21": [467.78], "50": [439.7]},
                                    "ma": {"20": [472.29], "50": [419.09], "200": [429.43]}},
                },
                "b": {
                    "method": "erick", "label": "Método Erick · diário", "verdict": "Buy",
                    "erick_report": "Erick: recuo à média nas EMAs 8/21.",
                    "actionable": {"as_of": "2026-08-24", "price": 488.88},
                    "price_chart": {"ema": {"8": [485.21], "21": [469.7], "50": [441.63]},
                                    "ma": {"20": [477.32], "50": [421.08], "200": [429.33]}},
                },
                "meta": {"agreement": "ambos compram", "divergence": "", "meaning": ""},
            },
        },
    }


# --- _num: padrão da casa (vírgula decimal, ponto de milhar) -----------------

@pytest.mark.parametrize("value,expected", [
    (136.93, "136,93"),
    (1234.5, "1.234,50"),
    (0, "0,00"),
    (None, None),
    ("", None),
    ("abc", None),
])
def test_num_pt_br_format(value, expected):
    assert A._num(value) == expected


def test_last_valid_skips_trailing_nulls():
    assert A._last_valid([1, 2, None]) == 2
    assert A._last_valid([None, None]) is None
    assert A._last_valid([]) is None
    assert A._last_valid(None) is None


# --- price_facts: o coração do grounding ------------------------------------

def test_price_facts_surfaces_real_ema_numbers():
    r = _single_record()["result"]
    facts = A.price_facts(r["actionable"], r["price_chart"])
    blob = "\n".join(facts)
    # o "recuo à média" pedido no brief: EMA 8/21 com número real (último da série)
    assert "EMA 8: 136,93" in blob
    assert "EMA 21: 134,07" in blob
    assert "Preço no momento da análise: 135,00" in blob
    assert "gatilho em 104,83" in blob


def test_price_facts_marks_absent_average_sem_dado():
    """MMS 200 sem histórico deve virar 'sem dado' — e NÃO pegar o número vizinho
    (bug real observado: modelo reportava a MMS 50 como se fosse a MMS 200)."""
    r = _single_record()["result"]
    facts = A.price_facts(r["actionable"], r["price_chart"])
    blob = "\n".join(facts)
    assert "MMS 200: sem dado" in blob
    assert "MMS 50: 141,50" in blob
    # o valor da MMS 50 não pode aparecer rotulado como MMS 200
    assert "MMS 200: 141,50" not in blob


def test_price_facts_empty_when_no_numbers():
    # sem preço, sem médias, zonas nulas -> nada pra ancorar -> [] (segue honesto)
    facts = A.price_facts(
        {"buy_zone": None, "realize_zone": None, "pullback_zone": None}, {}
    )
    assert facts == []


def test_price_facts_zone_without_basis_is_not_a_number():
    facts = A.price_facts({"price": None, "buy_zone": {"label": "x", "price": None}}, {})
    assert facts == []


# --- build_context: single e compare ----------------------------------------

def test_build_context_single_has_numbers():
    ctx = A.build_context(_single_record())
    assert ctx["mode"] == "single"
    assert ctx["has_numbers"] is True
    assert ctx["ticker"] == "SPCX"
    assert ctx["as_of"] == "2026-08-24"
    assert "EMA 8: 136,93" in ctx["facts"]
    # relatórios entram no contexto pra dar cor à resposta
    assert "recuo à média" in ctx["reports"]


def test_build_context_compare_names_both_columns():
    ctx = A.build_context(_compare_record())
    assert ctx["mode"] == "compare"
    assert ctx["has_numbers"] is True
    assert "Padrão · diário" in ctx["facts"]
    assert "Método Erick · diário" in ctx["facts"]
    # cada coluna traz suas próprias EMAs (não se misturam)
    assert "EMA 8: 484,17" in ctx["facts"]   # Padrão
    assert "EMA 8: 485,21" in ctx["facts"]   # Erick


def test_build_context_single_no_actionable_is_honest():
    rec = {"run_id": "x", "ticker": "AAA", "result": {"verdict": "Hold"}}
    ctx = A.build_context(rec)
    assert ctx["mode"] == "single"
    assert ctx["has_numbers"] is False


# --- build_messages: prompt + meta ------------------------------------------

def test_build_messages_shape_and_grounding_rules():
    messages, meta = A.build_messages(_single_record(), "onde seria o recuo à média?")
    assert [m[0] for m in messages] == ["system", "human"]
    system, human = messages[0][1], messages[1][1]
    # a regra anti-invenção precisa estar no sistema
    assert "JAMAIS invente" in system
    assert "sem dado" in system
    # o turno do usuário carrega os DADOS ancorados + a pergunta literal
    assert "EMA 8: 136,93" in human
    assert "onde seria o recuo à média?" in human
    assert meta["mode"] == "single"
    assert meta["has_numbers"] is True


def test_build_messages_truncates_long_reports():
    rec = _single_record()
    rec["result"]["market_report"] = "x" * 5000
    _, _ = A.build_messages(rec, "e aí?")
    ctx = A.build_context(rec)
    assert "[…]" in ctx["reports"]
    assert len(ctx["reports"]) < 5000


# --- runner.ask: encanamento (LLM falso, sem rede) --------------------------

class _FakeReply:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content="Resposta ancorada: EMA 8 em 136,93."):
        self.content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return _FakeReply(self.content)


def _runner_with(tmp_path, record):
    store = HistoryStore(tmp_path / "webui")
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "quick_think_llm": "gpt-4o-mini", "temperature": 0.0},
        store=store,
    )
    store.save(record)
    return runner


def test_ask_returns_answer_and_meta(tmp_path, monkeypatch):
    runner = _runner_with(tmp_path, _single_record())
    fake = _FakeLLM()
    monkeypatch.setattr(runner, "_answer_llm", lambda cbs, ov=None: fake)
    out = runner.ask("r-single", "onde seria o recuo à média?")
    assert out["answer"] == "Resposta ancorada: EMA 8 em 136,93."
    assert out["mode"] == "single"
    assert out["grounded"] is True
    assert out["model"] == "gpt-4o-mini"
    # o LLM recebeu os DADOS ancorados
    assert any("EMA 8: 136,93" in m[1] for m in fake.seen)


def test_ask_unknown_run_is_none(tmp_path, monkeypatch):
    runner = _runner_with(tmp_path, _single_record())
    monkeypatch.setattr(runner, "_answer_llm", lambda cbs, ov=None: _FakeLLM())
    assert runner.ask("nao-existe", "qualquer coisa") is None


def test_ask_empty_question_raises(tmp_path):
    runner = _runner_with(tmp_path, _single_record())
    with pytest.raises(ValueError):
        runner.ask("r-single", "   ")


def test_ask_joins_list_content_parts(tmp_path, monkeypatch):
    runner = _runner_with(tmp_path, _single_record())
    parts = [{"text": "parte 1 "}, {"text": "parte 2"}]
    monkeypatch.setattr(runner, "_answer_llm", lambda cbs, ov=None: _FakeLLM(parts))
    out = runner.ask("r-single", "e aí?")
    assert out["answer"] == "parte 1 parte 2"
