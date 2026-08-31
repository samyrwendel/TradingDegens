"""Busca INCREMENTAL do diário — e a integridade antes da economia (task 20260831-019).

Pedido do Samyr: *"sempre com dados incrementais, nada revalidar 123 pedindo todo
histórico que vc já tem em cache, vc tem os últimos dias e horas"*.

O que havia: toda revalidação do diário rebaixava a janela de **5 anos** para
obter a barra do dia (~110 KB por ativo). Com 20 ativos e a revalidação automática
por fechamento de candle (DA-118), isso multiplicaria por 24 no dia — contra um
provedor cujo throttle já custou um outlier de 75s, a partir do único IP
disponível, que é o de PRODUÇÃO.

**A trava que manda aqui é integridade, não economia.** Este produto inteiro é
nível calculado em cima de série: uma série remendada errada não dá erro, dá
número errado em todos os níveis, calado. Então o remendo exige **prova de
continuidade** — pede-se a partir do último dia QUE JÁ ESTÁ NO CACHE, e a
sobreposição resultante é a prova de que não há buraco. Sem ela, download
completo, com o motivo escrito.

Os dentes, um por modo de errar:

* **a barra do dia é MUTÁVEL** — duas leituras no mesmo dia não podem deixar a
  série com a linha duplicada, nem congelada no valor da primeira;
* **buraco não se costura em silêncio** — trecho novo que começa depois do fim do
  cache derruba o incremental e cai no completo;
* **o pedido é pequeno** — o incremental pede a partir do último dia do cache,
  não do começo da janela (é isto que o defeito fazia);
* **fora do pregão não há barra nova** — e isso não pode virar erro nem
  re-download; o arquivo é tocado pra a TTL não bater na fonte a cada chamada;
* **as duas cópias do `load_ohlcv`** (o módulo e o wrapper do datacache) usam a
  MESMA função de emenda; a segunda cópia é o jeito clássico de a série divergir.
"""

import pandas as pd
import pytest

from tradingagents.dataflows import stockstats_utils as ssu

pytestmark = pytest.mark.unit


def _serie(inicio, n, base=100.0):
    dias = pd.bdate_range(inicio, periods=n)
    return pd.DataFrame({
        "Date": dias,
        "Open": [base + i for i in range(n)],
        "High": [base + i + 1 for i in range(n)],
        "Low": [base + i - 1 for i in range(n)],
        "Close": [base + i + 0.5 for i in range(n)],
        "Volume": [1000 + i for i in range(n)],
    })


@pytest.fixture
def espia(monkeypatch):
    """Substitui o download e registra COM QUE `start` cada chamada foi feita."""
    chamadas = []
    resposta = {"frame": _serie("2026-01-01", 0)}

    class _FakeYF:
        @staticmethod
        def download(sym, start=None, end=None, **kw):
            chamadas.append({"start": start, "end": end})
            f = resposta["frame"]
            return f.set_index("Date") if not f.empty else f

    # Troca o NOME `yf` do módulo, não `sys.modules`: o download usa o import de
    # topo (um `import` local dentro da função seria uma segunda referência ao
    # mesmo pacote só para agradar ao teste).
    monkeypatch.setattr(ssu, "yf", _FakeYF)
    return {"chamadas": chamadas, "resposta": resposta}


# ------------------------------------------------------------- a emenda pura ----
def test_a_emenda_substitui_por_data_e_nao_duplica():
    """A barra do dia corrente é mutável: a segunda leitura SOBRESCREVE a primeira."""
    cache = _serie("2026-08-24", 5)                    # 24..28/08
    novo = _serie("2026-08-28", 2, base=900.0)         # 28..31/08, valores outros
    out = ssu.emenda_ohlcv(cache, novo)

    assert out["Date"].is_unique, "a emenda duplicou linha"
    assert out["Date"].is_monotonic_increasing
    # o dia que existia nos dois lados fica com o valor NOVO
    dia = pd.Timestamp("2026-08-28")
    assert float(out.loc[out["Date"] == dia, "Close"].iloc[0]) == 900.5
    # e o histórico anterior ao corte permanece intacto
    assert len(out[out["Date"] < dia]) == 4


def test_a_emenda_aguenta_o_CACHE_VINDO_DE_CSV(tmp_path):
    """A forma REAL da produção: o cache tem `Date` string, o download tem Timestamp.

    DENTE que a suíte não pegava porque todo teste montava o cache em memória: a
    primeira versão desta função concatenava as duas pontas cruas e o
    `sort_values` seguinte levantava
    `TypeError: '<' not supported between Timestamp and str` — em produção, na
    primeira revalidação de verdade.
    """
    csv = tmp_path / "c.csv"
    _serie("2026-08-01", 20).to_csv(csv, index=False)
    cache = pd.read_csv(csv)                       # Date volta como TEXTO
    assert not pd.api.types.is_datetime64_any_dtype(cache["Date"]), cache["Date"].dtype

    novo = _serie(pd.to_datetime(cache["Date"].iloc[-1]), 1, base=900.0)
    out = ssu.emenda_ohlcv(cache, novo)
    assert len(out) == 20 and out["Date"].is_unique
    assert out["Date"].is_monotonic_increasing
    assert float(out["Close"].iloc[-1]) == 900.5


def test_a_emenda_preserva_o_historico_anterior_ao_corte():
    cache = _serie("2026-01-01", 100)
    novo = _serie(cache["Date"].iloc[-1], 3, base=500.0)
    out = ssu.emenda_ohlcv(cache, novo)
    assert len(out) == 99 + 3
    assert float(out["Close"].iloc[0]) == float(cache["Close"].iloc[0])


# ------------------------------------------------------- a decisão de baixar ----
def test_sem_cache_baixa_COMPLETO(espia):
    espia["resposta"]["frame"] = _serie("2021-01-01", 10)
    _f, modo, motivo = ssu.busca_ohlcv("AAPL", "2021-08-31", "2026-09-01", cached=None)
    assert modo == "completo" and "não havia série" in motivo
    assert espia["chamadas"][0]["start"] == "2021-08-31"


def test_com_cache_pede_SO_do_ultimo_dia_conhecido(espia):
    """O defeito era este: pedir 5 anos por causa da barra do dia."""
    cache = _serie("2026-08-01", 20)                    # termina em 28/08
    fim = cache["Date"].iloc[-1]
    espia["resposta"]["frame"] = _serie(fim, 2, base=900.0)

    _f, modo, _m = ssu.busca_ohlcv("AAPL", "2021-08-31", "2026-09-01", cached=cache)
    assert modo == "incremental"
    assert espia["chamadas"][0]["start"] == fim.strftime("%Y-%m-%d"), espia["chamadas"]
    assert espia["chamadas"][0]["start"] != "2021-08-31"


def test_duas_buscas_no_mesmo_dia_nao_deixam_linha_repetida_nem_congelada(espia):
    """O teste que o critério pede, ponta a ponta pela `busca_ohlcv`."""
    cache = _serie("2026-08-01", 20)
    fim = cache["Date"].iloc[-1]

    # 1ª leitura do dia: candle parcial
    espia["resposta"]["frame"] = _serie(fim, 1, base=700.0)
    f1, _m, _ = ssu.busca_ohlcv("AAPL", "2021-08-31", "2026-09-01", cached=cache)
    assert float(f1.loc[f1["Date"] == fim, "Close"].iloc[0]) == 700.5

    # 2ª leitura, mais tarde: o MESMO dia com outro valor
    espia["resposta"]["frame"] = _serie(fim, 1, base=800.0)
    f2, _m, _ = ssu.busca_ohlcv("AAPL", "2021-08-31", "2026-09-01", cached=f1)
    assert f2["Date"].is_unique, "a segunda leitura duplicou o dia"
    assert float(f2.loc[f2["Date"] == fim, "Close"].iloc[0]) == 800.5, \
        "o candle do dia ficou congelado no valor da primeira leitura"
    assert len(f2) == len(cache)


def test_BURACO_entre_o_cache_e_o_trecho_novo_cai_no_completo_e_diz_por_que(espia):
    """DENTE: costurar aqui inventaria continuidade que ninguém verificou.

    O trecho novo começa DEPOIS do fim do cache — os dias do meio não foram
    olhados por ninguém. Emendar produziria uma série com um salto invisível, e
    todo nível calculado em cima dela sairia errado sem dar erro.
    """
    cache = _serie("2026-08-01", 10)                    # termina em 14/08
    espia["resposta"]["frame"] = _serie("2026-08-25", 5)  # começa 11 dias depois
    _f, modo, motivo = ssu.busca_ohlcv("AAPL", "2021-08-31", "2026-09-01", cached=cache)
    assert modo == "completo", "costurou um buraco em silêncio"
    assert "buraco" in motivo and "2026-08" in motivo, motivo
    # e o completo foi de fato pedido do começo da janela
    assert espia["chamadas"][-1]["start"] == "2021-08-31"


def test_cache_inteiro_fora_da_janela_de_5_anos_cai_no_completo(espia):
    cache = _serie("2015-01-01", 10)
    espia["resposta"]["frame"] = _serie("2021-08-31", 100)
    _f, modo, motivo = ssu.busca_ohlcv("AAPL", "2021-08-31", "2026-09-01", cached=cache)
    assert modo == "completo" and "5 anos" in motivo


def test_sem_barra_nova_devolve_o_CACHE_e_nao_erro_nem_redownload(espia):
    """Fora do pregão este é o caso normal, e é o que mais economiza chamada."""
    cache = _serie("2026-08-01", 20)
    espia["resposta"]["frame"] = _serie("2026-01-01", 0)   # vazio
    f, modo, _m = ssu.busca_ohlcv("AAPL", "2021-08-31", "2026-09-01", cached=cache)
    assert modo == "sem_novidade"
    assert f is cache
    assert len(espia["chamadas"]) == 1, "pediu de novo depois de não haver novidade"


# --------------------------------------------------- as duas cópias do caminho ----
def test_o_wrapper_do_datacache_usa_a_MESMA_emenda_do_modulo():
    """Duas políticas de download divergiriam justo na parte que produz série errada."""
    import inspect

    from tradingagents.datacache import patch

    src = inspect.getsource(patch._make_stable_load_ohlcv)
    assert "mod.busca_ohlcv" in src, "o wrapper voltou a ter download próprio"
    assert "yf.download" not in src, "o wrapper tem uma segunda política de download"


def test_o_nome_do_arquivo_de_cache_NAO_depende_da_data(tmp_path, monkeypatch):
    """A chave datada era cache miss garantido a cada virada de dia.

    O wrapper do datacache já estabiliza o nome (`-YFin-5y.csv`); este teste é o
    guarda de que ele continua estabilizando — e nomeia a armadilha pra quem
    voltar aqui.
    """
    import inspect

    from tradingagents.datacache import patch

    src = inspect.getsource(patch._make_stable_load_ohlcv)
    assert '-YFin-5y.csv' in src
    assert '-YFin-data-{' not in src and "-YFin-data-%s" not in src
