"""A CARTEIRA DO ERICK dentro do produto — e o portão que a protege (DA-148).

Duas travas, e nenhuma é negociável:

1. **SÓ-DONO, sem alternativa de BYOK.** Todo o resto que o produto lê é público —
   qualquer visitante poderia buscar o mesmo dado. Isto não: é conteúdo de
   assinatura paga, e a tela de login do autor diz "acesso exclusivo para alunos".
   O Samyr é aluno e pode consumir o que comprou; o visitante do produto dele, não.
   Trazer chave própria de LLM não compra assinatura de terceiro, então o portão de
   CUSTO (`_gate_or_403`) não serve aqui — só o de AUTORIZAÇÃO.
2. **A credencial não mora no código.** Sem `ERICK_CARTEIRA_EMAIL` no ambiente, a
   feature não existe: a rota devolve 404 e o front esconde o botão. E o e-mail
   NUNCA aparece em resposta, em log ou em mensagem de erro.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from tradingagents.dataflows import erick_carteira as ec
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore

pytestmark = pytest.mark.unit


_AMOSTRA = {
    "atualizado": "27/08/2026",
    "aporteInicial": 70000,
    "ativos": [
        {"ticker": "MSFT", "nome": "Microsoft", "classe": "Acao", "qtd": 22.108,
         "precoMedio": 381.93, "entrada": "jul/2026", "tese": "posição maior"},
        {"ticker": "BTC", "nome": "Bitcoin", "classe": "Cripto", "qtd": 0.0084,
         "precoMedio": 62485.88, "entrada": "jul/2026", "tese": "reserva digital"},
        {"ticker": "CASH", "nome": "Caixa", "classe": "Caixa", "qtd": 84829.22,
         "precoMedio": 1, "entrada": "-", "tese": "Caixa elevado por escolha. Reserva."},
    ],
    "feed": [{"id": 1, "tipo": "venda", "titulo": "Saída de IREN", "data": "27/08/2026"}],
    "relatorios": [{"id": 1, "titulo": "Fechamento", "data": "2026-07-29", "texto": "x"}],
}


@pytest.fixture
def servidor(tmp_path):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path))
    httpd = make_server("127.0.0.1", 0, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", runner
    finally:
        httpd.shutdown()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        corpo = e.read().decode()
        try:
            return e.code, json.loads(corpo)
        except ValueError:
            return e.code, corpo


def test_DENTE_visitante_anonimo_leva_403_e_a_rota_nao_executa_nada(servidor, monkeypatch):
    """O portão vem ANTES de qualquer leitura: um 403 que primeiro busca a carteira
    já teria buscado conteúdo de assinatura pra quem não assinou."""
    base, _ = servidor
    monkeypatch.setattr(ec, "carteira",
                        lambda **kw: pytest.fail("a rota LEU a carteira antes do portão"))
    status, corpo = _get(f"{base}/api/erick/carteira")
    assert status == 403, (status, corpo)
    assert corpo.get("error_code") == "owner_only", corpo


def test_o_403_nao_vaza_nem_o_endereco_nem_quem_assina(servidor):
    base, _ = servidor
    _, corpo = _get(f"{base}/api/erick/carteira")
    texto = json.dumps(corpo, ensure_ascii=False)
    assert "ericksekiama" not in texto, texto
    assert "@" not in texto, texto


def test_sem_credencial_a_feature_NAO_EXISTE_nesta_instancia(monkeypatch):
    """Falha silenciosa e limpa: `None`, não exceção. Um stack trace aqui exporia o
    endereço de alguém, e um 500 anunciaria que a feature existe."""
    monkeypatch.delenv("ERICK_CARTEIRA_EMAIL", raising=False)
    assert ec.configurado() is False
    assert ec.carteira() is None


def test_o_cache_responde_dentro_da_janela_e_NAO_bate_no_servidor_alheio(monkeypatch, tmp_path):
    """Cadência civilizada: é servidor de outra pessoa e o dado é atualizado à mão.
    DENTE: qualquer regressão que volte a buscar a cada pedido bate aqui."""
    monkeypatch.setenv("ERICK_CARTEIRA_EMAIL", "alguem@exemplo.com")
    monkeypatch.setattr(ec, "_CACHE", tmp_path / "c.json")
    n = {"i": 0}

    def busca_falsa():
        n["i"] += 1
        return {"carteira": _AMOSTRA, "historico": None, "lido_em": __import__("time").time()}

    monkeypatch.setattr(ec, "_busca", busca_falsa)
    primeira = ec.carteira()
    segunda = ec.carteira()
    assert n["i"] == 1, "a segunda leitura foi ao servidor alheio dentro da janela"
    assert primeira["carteira"]["atualizado"] == "27/08/2026"
    assert segunda["degradado"] is False


def test_falha_de_acesso_DEGRADA_pro_ultimo_lido_em_vez_de_esvaziar_a_tela(monkeypatch, tmp_path):
    """Painel vazio se leria como "ele zerou a carteira" — que é uma AFIRMAÇÃO, não
    uma ausência de dado. Degrada mostrando o último lido, marcado."""
    monkeypatch.setenv("ERICK_CARTEIRA_EMAIL", "alguem@exemplo.com")
    monkeypatch.setattr(ec, "_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(ec, "_busca", lambda: {
        "carteira": _AMOSTRA, "historico": None, "lido_em": 1_000.0})
    assert ec.carteira() is not None            # semeia o cache (lido_em bem velho)

    def explode():
        raise RuntimeError("site fora do ar")
    monkeypatch.setattr(ec, "_busca", explode)
    d = ec.carteira()
    assert d is not None and d["degradado"] is True
    assert d["carteira"]["atualizado"] == "27/08/2026"
    assert d["idade_horas"] and d["idade_horas"] > 1, d


def test_sem_cache_e_com_a_fonte_fora_do_ar_devolve_None_em_vez_de_inventar(monkeypatch, tmp_path):
    monkeypatch.setenv("ERICK_CARTEIRA_EMAIL", "alguem@exemplo.com")
    monkeypatch.setattr(ec, "_CACHE", tmp_path / "vazio.json")

    def explode():
        raise RuntimeError("site fora do ar")
    monkeypatch.setattr(ec, "_busca", explode)
    assert ec.carteira() is None


def test_a_participacao_sai_do_preco_MEDIO_e_o_caixa_entra_na_conta(monkeypatch):
    """Misturar a cotação de agora aqui produziria um número que não bate com a foto
    que o próprio autor publica. E o caixa É posição — fora da conta, os 71% dele
    virariam 100% de outra coisa."""
    linhas = ec.composicao({"carteira": _AMOSTRA})
    porc = {L["ticker"]: L["participacao"] for L in linhas}
    assert abs(sum(porc.values()) - 1.0) < 1e-9, porc
    assert porc["CASH"] > porc["MSFT"] > porc["BTC"], porc
    # a maior posição é o CAIXA, e é isso que o painel precisa poder dizer
    assert porc["CASH"] > 0.6, porc


@pytest.mark.parametrize("linha,esperado", [
    ({"ticker": "MSFT", "classe": "Acao"}, "MSFT"),
    ({"ticker": "BTC", "classe": "Cripto"}, "BTC-USD"),
    ({"ticker": "CASH", "classe": "Caixa"}, None),
    ({"ticker": "", "classe": "Acao"}, None),
])
def test_o_simbolo_traduz_pro_vocabulario_do_PRODUTO_e_caixa_nao_vira_ticker(linha, esperado):
    """A cripto vem no formato da exchange no payload dele; o produto fala BTC-USD.
    E CAIXA não é ativo negociável — pedir cotação de "CASH" seria inventar símbolo."""
    assert AnalysisRunner._erick_simbolo(linha) == esperado
