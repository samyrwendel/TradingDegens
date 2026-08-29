"""WatchlistStore — o que a UI recebe tem que ser o que foi pro disco.

Limpeza da revisão: ``add`` gravava ``current[:100]`` e RETORNAVA ``current``
inteiro. O 101º ticker aparecia na lista e sumia no reload — a tela mentindo
sobre o que tinha sido salvo, que é o pior tipo de bug de persistência: nada
quebra, só some.
"""
import pytest

from tradingagents.webui.store import HistoryStore, WatchlistStore

pytestmark = pytest.mark.unit


@pytest.fixture()
def store(tmp_path):
    return WatchlistStore(tmp_path, HistoryStore(tmp_path))


def test_add_devolve_exatamente_o_que_persistiu(store):
    for i in range(105):
        devolvido = store.add(f"TK{i:03d}")
    assert len(devolvido) == 100, "a UI recebeu mais do que cabe no disco"
    assert devolvido == store.get(), "o que a tela mostra ≠ o que o reload traz"


def test_o_101o_ticker_nao_aparece_e_some(store):
    for i in range(100):
        store.add(f"TK{i:03d}")
    devolvido = store.add("EXTRA")
    # entra no topo (é o mais novo) e o mais antigo é que cai — mas o total é 100
    assert len(devolvido) == 100
    assert devolvido[0]["ticker"] == "EXTRA"
    assert store.get()[0]["ticker"] == "EXTRA"
    assert [w["ticker"] for w in devolvido] == [w["ticker"] for w in store.get()]


def test_duplicado_nao_reescreve_nem_duplica(store):
    store.add("AAPL")
    antes = store.get()
    assert store.add("aapl") == antes
    assert [w["ticker"] for w in store.get()].count("AAPL") == 1
