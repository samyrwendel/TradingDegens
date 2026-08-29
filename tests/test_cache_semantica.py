"""Disciplina do ``_SEMANTICA_KEY`` — e a armadilha que sobrou armada na irmã.

O C3 versionou a chave do ``earnings_next`` porque o significado da resposta mudou
e entrada de data passada é gravada PERMANENTE: sem o bump, toda (símbolo, data) já
consultada continuaria devolvendo a resposta errada PRA SEMPRE, inclusive em
backtest. O ``earnings_reported`` (Finnhub) tem a mesma forma — chave por
(categoria, símbolo, data), escrita permanente — e ficou sem versão nenhuma.

Aqui trava-se: (a) a entrada envenenada da semântica antiga não é servida, nas duas
categorias; (b) versionar não desligou o cache; (c) o purge existe pra as órfãs;
(d) o guardrail estrutural — nenhuma chave de cache de dataflows escrita com
categoria fixa pode existir sem uma versão de semântica, que era a parte que só
vivia como comentário.
"""
import ast
import pathlib

import pytest

from tradingagents.datacache import cache
from tradingagents.dataflows import finnhub_earnings as fe

pytestmark = pytest.mark.unit


@pytest.fixture()
def cache_real(tmp_path, monkeypatch):
    """Liga o cache de verdade num diretório temporário (o autouse o desliga)."""
    monkeypatch.setattr(cache, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "DISABLED", False)
    return cache


_HISTORY = [
    {"period": "2026-06-30", "actual": 1.87, "estimate": 1.79, "surprise": 0.08,
     "surprisePercent": 4.34, "quarter": 1, "year": 2027},
]


@pytest.fixture()
def fonte(monkeypatch):
    monkeypatch.setattr(fe, "get_api_key", lambda: "TESTKEY")
    monkeypatch.setattr(fe, "_fetch_announce_date", lambda symbol, period_end: None)
    monkeypatch.setattr(fe, "_fetch_recent_announcement", lambda symbol, base: None)
    monkeypatch.setattr(fe, "_fetch_surprise_history", lambda symbol: [dict(r) for r in _HISTORY])


def test_entrada_reportada_da_semantica_ANTIGA_nao_e_servida(cache_real, fonte):
    """Mesmo teste do C3, na categoria que tinha ficado de fora.

    DENTE: sem ``_SEMANTICA_KEY`` na chave, o EPS envenenado (99.99) volta.
    """
    base = "2026-10-01"
    envenenada = {"symbol": "NVDA", "period": "2000-01-01", "eps_actual": 99.99,
                  "eps_estimate": 0.01, "beat": True, "recent": True, "days_since": 0}
    k_antiga = cache_real.key(fe._CATEGORY, "NVDA", base)
    cache_real.set_ok(fe._CATEGORY, k_antiga, envenenada, True)
    assert cache_real.get(fe._CATEGORY, k_antiga) is not None      # está lá, permanente

    ev = fe.get_reported_earnings("NVDA", base)
    assert ev is not None
    assert ev["eps_actual"] != 99.99, ("a entrada velha voltou a ser servida", ev)
    assert ev["period"] == "2026-06-30"


def test_o_cache_novo_e_usado_de_verdade(cache_real, fonte, monkeypatch):
    """Contra-prova: versionar a chave não pode ter DESLIGADO o cache."""
    base = "2026-10-01"
    primeiro = fe.get_reported_earnings("NVDA", base)

    def nunca(symbol):
        raise AssertionError("bateu na fonte de novo — o cache novo não pegou")

    monkeypatch.setattr(fe, "_fetch_surprise_history", nunca)
    assert fe.get_reported_earnings("NVDA", base) == primeiro


def test_purge_apaga_a_categoria_inteira(cache_real):
    """As órfãs de uma semântica morta ficavam no disco pra sempre (permanentes
    nunca expiram, e a chave é hash — nada as encontra). O purge é a saída."""
    for i in range(3):
        cache_real.set_ok("earnings_reported", cache_real.key("x", i), {"v": i}, True)
    cache_real.set_ok("outra_categoria", cache_real.key("y"), {"v": 1}, True)

    assert cache_real.purge_category("earnings_reported") == 3
    assert cache_real.purge_category("earnings_reported") == 0        # idempotente
    assert cache_real.get("outra_categoria", cache_real.key("y")) is not None
    assert cache_real.purge_category("categoria_que_nao_existe") == 0  # fail-open


# ------------------------------------------------------- guardrail estrutural ---
def _chaves_de_cache_sem_versao() -> list[str]:
    """Chamadas de chave de cache, em dataflows, sem uma versão de semântica.

    Procura ``cache.key(...)`` e as tuplas de partes passadas ao ``_cached_call``.
    Chamada com ``*args`` é o wrapper genérico (recebe as partes de quem chama) e
    não é auditável aqui — o que se audita é quem MONTA a tupla.
    """
    raiz = pathlib.Path(__file__).resolve().parents[1] / "tradingagents" / "dataflows"
    faltando = []
    for arquivo in sorted(raiz.rglob("*.py")):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
        for no in ast.walk(arvore):
            if not isinstance(no, ast.Call):
                continue
            f = no.func
            partes = None
            if isinstance(f, ast.Attribute) and f.attr == "key" and \
                    isinstance(f.value, ast.Name) and f.value.id == "cache":
                partes = list(no.args)
            elif isinstance(f, ast.Name) and f.id == "_cached_call" and len(no.args) >= 2 \
                    and isinstance(no.args[1], ast.Tuple):
                partes = list(no.args[1].elts)
            if partes is None:
                continue
            if any(isinstance(a, ast.Starred) for a in partes):
                continue                                   # wrapper genérico
            nomes = [a.id for a in partes if isinstance(a, ast.Name)]
            if not any("SEMANTICA" in n for n in nomes):
                faltando.append(f"{arquivo.name}:{no.lineno}")
    return faltando


def test_toda_chave_de_cache_de_dataflows_carrega_versao_de_semantica():
    """A disciplina "mudou a semântica → bump" existia só como COMENTÁRIO, e foi
    exatamente assim que o ``earnings_reported`` ficou sem versão enquanto a irmã
    ganhava a dela. Agora um cache novo sem versão quebra teste na hora de nascer.
    """
    faltando = _chaves_de_cache_sem_versao()
    assert not faltando, (
        "chave de cache sem versão de semântica (entrada histórica é PERMANENTE — "
        "mudar o significado sem bumpar grava o erro pra sempre): " + ", ".join(faltando))
