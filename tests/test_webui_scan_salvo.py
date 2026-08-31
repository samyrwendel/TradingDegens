"""O ÚLTIMO CONHECIDO da watchlist — por ativo, em disco, alimentado por TODA
varredura (tasks 20260831-014, 015 e 016).

O painel abria VAZIO os 8–20s da varredura porque nada no servidor guardava "o
último resultado". Havia três candidatos e nenhum servia: o ``_scan_memo`` do
runner (5s, em memória), o ``_live_cache`` do scanner (30s, em memória) e o
``scans.jsonl`` (em disco, mas append-only e só dos ``em_gatilho`` — é um ledger
de gatilhos, não uma varredura). Enquanto isso a agenda varria de hora em hora e
**jogava o resultado fora**: o dado que a tela precisava já era produzido e
descartado.

A forma final (016) NÃO é "o último scan completo", é **o último conhecido POR
ATIVO** — porque a passada da agenda é parcial por desenho (cripto sempre, ação
só com pregão aberto). Os dentes daqui, um por armadilha real:

* **restart não perde** — uma instância NOVA lê o que a anterior gravou; é o que
  separa disco de memória, e o que o ``_scan_memo`` nunca poderia fazer.
* **passada parcial não se apresenta como completa** — ela declara ``completa:
  False``, a sessão de mercado e quais tickers cobriu. Servi-la calada mostraria
  meia watchlist como se fosse a lista toda.
* **ativo fora da passada fica com a hora DELE** — não some (perderia informação)
  e não herda o carimbo novo (mentiria sobre a idade).
* **um só ponto de escrita** — tela e agenda gravam pelo mesmo caminho; dois
  divergiriam justo na parte difícil, a fusão por ativo.
* **ativo removido da watchlist sai** — senão um ticker apagado ficaria pra
  sempre, porque passada parcial nunca o mencionaria de novo.
* **vazio não vira "salvo"** — lista vazia se lê como "não há nada em gatilho",
  que é uma afirmação, não uma ausência de dado.
* **fail-open** — arquivo corrompido/ausente devolve ``{}`` e nunca explode.
"""

import json
import threading
from datetime import datetime, timedelta

import pytest

from tradingagents.webui import scanner, timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.scanner import ordenar_e_resumir
from tradingagents.webui.store import HistoryStore, ScanSnapshotStore

pytestmark = pytest.mark.unit

_T0 = "2026-08-31T10:00:00-04:00"
_T1 = "2026-08-31T14:32:00-04:00"


def _ativo(ticker, estado="formando", **kw):
    melhor = {"frame": "1d", "estado": estado, "direction": "compra",
              "price": 513.53, "trigger": 512.76, **kw}
    return {"ticker": ticker, "melhor": melhor, "frames": [melhor]}


def _passada(tickers, gerado_em=_T1, estado="formando"):
    return {"date": "2026-08-31", "frames": ["1d", "4h", "1h"],
            "resumo": {}, "gerado_em": gerado_em,
            "ativos": [_ativo(t, estado) for t in tickers]}


def _reg(store, resultado, universo, **kw):
    kw.setdefault("completa", True)
    return store.registrar(resultado, universo=universo,
                           ordenar_e_resumir=ordenar_e_resumir, **kw)


# ------------------------------------------------------------------ o store ----
def test_grava_e_le_de_volta(tmp_path):
    _reg(ScanSnapshotStore(tmp_path), _passada(["MSFT"]), ["MSFT"])
    got = ScanSnapshotStore(tmp_path).get()
    assert got["ativos"][0]["ticker"] == "MSFT"
    assert got["gerado_em"] == _T1
    assert got["ultima_passada"]["completa"] is True


def test_sem_nada_salvo_devolve_vazio_e_nao_explode(tmp_path):
    assert ScanSnapshotStore(tmp_path).get() == {}


def test_arquivo_corrompido_devolve_vazio(tmp_path):
    store = ScanSnapshotStore(tmp_path)
    _reg(store, _passada(["MSFT"]), ["MSFT"])
    store.path.write_text("{isto não é json", encoding="utf-8")
    assert ScanSnapshotStore(tmp_path).get() == {}


def test_varredura_sem_ativos_nao_vira_salvo(tmp_path):
    store = ScanSnapshotStore(tmp_path)
    vazia = _passada([])
    assert _reg(store, vazia, []) == {}
    assert store.get() == {}


def test_passada_PARCIAL_nao_apaga_quem_ela_nao_varreu(tmp_path):
    """O dente central da 016: a fusão é por ATIVO.

    Uma passada de madrugada lê só as criptos. Se ela virasse "o último scan", a
    abertura mostraria 1 de 3 ativos sem dizer — meia watchlist com cara de
    watchlist inteira.
    """
    store = ScanSnapshotStore(tmp_path)
    universo = ["MSFT", "NVDA", "BTC-USD"]
    _reg(store, _passada(universo, gerado_em=_T0), universo)

    estado = _reg(store, _passada(["BTC-USD"], gerado_em=_T1, estado="em_gatilho"),
                  universo, completa=False, sessao="fechada")

    por_ticker = {a["ticker"]: a for a in estado["ativos"]}
    assert set(por_ticker) == set(universo), "a passada parcial apagou quem não varreu"
    # cada um com a hora DELE
    assert por_ticker["BTC-USD"]["gerado_em"] == _T1
    assert por_ticker["MSFT"]["gerado_em"] == _T0
    assert por_ticker["NVDA"]["gerado_em"] == _T0
    # o topo é a passada mais recente, e ela se DECLARA parcial
    assert estado["gerado_em"] == _T1
    assert estado["ultima_passada"] == {"gerado_em": _T1, "completa": False,
                                        "sessao": "fechada", "tickers": ["BTC-USD"],
                                        "universo": 3}
    # e o resumo conta o conjunto MESCLADO, não só o que a passada leu
    assert estado["resumo"] == {"em_gatilho": 1, "formando": 2}


def test_passada_nova_sobrescreve_o_ativo_que_ela_varreu(tmp_path):
    store = ScanSnapshotStore(tmp_path)
    _reg(store, _passada(["BTC-USD"], gerado_em=_T0, estado="formando"), ["BTC-USD"])
    estado = _reg(store, _passada(["BTC-USD"], gerado_em=_T1, estado="invalidou"),
                  ["BTC-USD"], completa=False)
    assert len(estado["ativos"]) == 1
    assert estado["ativos"][0]["melhor"]["estado"] == "invalidou"
    assert estado["ativos"][0]["gerado_em"] == _T1


def test_ativo_removido_da_watchlist_sai_do_ultimo_conhecido(tmp_path):
    """Sem a poda pelo universo, um ticker apagado ficaria pra sempre: passada
    parcial nunca o mencionaria de novo, e a fusão só sabe somar."""
    store = ScanSnapshotStore(tmp_path)
    _reg(store, _passada(["MSFT", "NVDA"], gerado_em=_T0), ["MSFT", "NVDA"])
    estado = _reg(store, _passada(["MSFT"], gerado_em=_T1), ["MSFT"], completa=True)
    assert [a["ticker"] for a in estado["ativos"]] == ["MSFT"]


def test_a_lista_mesclada_sai_na_ORDEM_da_varredura_viva(tmp_path):
    """Mesma ordenação dos dois lados — senão o mesmo portfólio pareceria dois.

    A abertura pinta o mesclado e a varredura pinta o vivo; ordens diferentes
    fariam a lista dar um salto quando a varredura chega, sem nada ter mudado.
    """
    store = ScanSnapshotStore(tmp_path)
    universo = ["AAA", "BBB", "CCC"]
    _reg(store, {"date": "2026-08-31", "frames": ["1d"], "gerado_em": _T0,
                 "ativos": [_ativo("AAA", "sem_setup"), _ativo("BBB", "em_gatilho"),
                            _ativo("CCC", "formando")]}, universo)
    ordem = [a["ticker"] for a in store.get()["ativos"]]
    assert ordem == ["BBB", "CCC", "AAA"], "urgência: em_gatilho, formando, sem_setup"


# ------------------------------------------------------ o carimbo do scanner ----
def test_scan_watchlist_carimba_gerado_em(monkeypatch):
    monkeypatch.setattr(scanner, "scan_symbol",
                        lambda t, d, f: {"ticker": t, "melhor": {"estado": "sem_setup"},
                                         "frames": []})
    antes = timeutil.now()
    r = scanner.scan_watchlist(["MSFT"], "2026-08-31")
    depois = timeutil.now()
    assert "gerado_em" in r, "sem carimbo a tela não sabe de quando é o dado"
    quando = datetime.fromisoformat(r["gerado_em"])
    assert quando.utcoffset() is not None, "carimbo tem que ser offset-aware"
    assert antes.replace(microsecond=0) <= quando <= depois


def test_scan_watchlist_vazia_tambem_carimba():
    r = scanner.scan_watchlist([], "2026-08-31")
    assert r["ativos"] == [] and "gerado_em" in r


# ------------------------------------------------------------------ o runner ----
def _runner(tmp_path):
    return AnalysisRunner(
        base_config={"results_dir": str(tmp_path), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path))


def test_scan_portfolio_grava_e_um_processo_NOVO_enxerga(tmp_path, monkeypatch):
    """O dente do restart: quem grava e quem lê são instâncias diferentes."""
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _passada(["NVDA"]))
    r1 = _runner(tmp_path)
    r1.watchlist_store.set(["NVDA"])
    assert r1.scan_ultimo() == {}, "nada salvo ainda"
    r1.scan_portfolio("2026-08-31")

    r2 = _runner(tmp_path)          # como se o serviço tivesse reiniciado
    salvo = r2.scan_ultimo()
    assert salvo["ativos"][0]["ticker"] == "NVDA"
    assert salvo["gerado_em"] == _T1
    assert salvo["ultima_passada"]["completa"] is True


def test_a_AGENDA_grava_o_resultado_em_vez_de_jogar_fora(tmp_path, monkeypatch):
    """O achado da 016: a passada agendada computava o scan inteiro e descartava.

    DENTE: na implementação antiga ``scan_ultimo()`` continuava ``{}`` depois de
    uma passada agendada — a agenda só devolvia contagens.
    """
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _passada(["BTC-USD"], estado="em_gatilho"))
    monkeypatch.setattr("tradingagents.webui.agenda.sessao_de_mercado",
                        lambda w, f: ("fechada", "MSFT"))
    monkeypatch.setattr("tradingagents.webui.agenda.alvos_da_passada",
                        lambda w, s: ["BTC-USD"])
    r = _runner(tmp_path)
    r.watchlist_store.set(["MSFT", "BTC-USD"])
    assert r.scan_ultimo() == {}

    r.scan_agendado()

    salvo = r.scan_ultimo()
    assert [a["ticker"] for a in salvo["ativos"]] == ["BTC-USD"]
    assert salvo["ultima_passada"]["completa"] is False, \
        "passada parcial não pode se apresentar como scan completo"
    assert salvo["ultima_passada"]["sessao"] == "fechada"
    assert salvo["ultima_passada"]["tickers"] == ["BTC-USD"]


def test_agenda_depois_da_tela_preserva_a_acao_com_a_hora_dela(tmp_path, monkeypatch):
    """O caso REAL de madrugada: tela varreu tudo às 10h, agenda leu só cripto às 14h."""
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _passada(["MSFT", "BTC-USD"], gerado_em=_T0))
    r = _runner(tmp_path)
    r.watchlist_store.set(["MSFT", "BTC-USD"])
    r.scan_portfolio("2026-08-31")

    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _passada(["BTC-USD"], gerado_em=_T1))
    monkeypatch.setattr("tradingagents.webui.agenda.sessao_de_mercado",
                        lambda w, f: ("fechada", "MSFT"))
    monkeypatch.setattr("tradingagents.webui.agenda.alvos_da_passada",
                        lambda w, s: ["BTC-USD"])
    r.scan_agendado()

    por_ticker = {a["ticker"]: a for a in r.scan_ultimo()["ativos"]}
    assert por_ticker["MSFT"]["gerado_em"] == _T0, "a ação herdou a hora da passada nova"
    assert por_ticker["BTC-USD"]["gerado_em"] == _T1
    assert r.scan_ultimo()["gerado_em"] == _T1


def test_UM_ponto_de_escrita_para_a_tela_e_para_a_agenda(tmp_path, monkeypatch):
    """Tela e agenda gravam pelo MESMO método — dois caminhos divergiriam na fusão."""
    chamadas: list[dict] = []
    r = _runner(tmp_path)
    r.watchlist_store.set(["MSFT", "BTC-USD"])
    original = r._guardar_ultimo
    monkeypatch.setattr(r, "_guardar_ultimo",
                        lambda res, **kw: (chamadas.append(kw), original(res, **kw))[1])
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _passada(["MSFT", "BTC-USD"]))
    r.scan_portfolio("2026-08-31")

    monkeypatch.setattr("tradingagents.webui.agenda.sessao_de_mercado",
                        lambda w, f: ("fechada", "MSFT"))
    monkeypatch.setattr("tradingagents.webui.agenda.alvos_da_passada",
                        lambda w, s: ["BTC-USD"])
    r.scan_agendado()

    assert [c["completa"] for c in chamadas] == [True, False]
    assert all(set(c["universo"]) == {"MSFT", "BTC-USD"} for c in chamadas), chamadas


def test_falha_de_disco_nao_derruba_a_varredura(tmp_path, monkeypatch):
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _passada(["NVDA"]))
    r = _runner(tmp_path)
    r.watchlist_store.set(["NVDA"])
    monkeypatch.setattr(r.scan_snapshot, "registrar",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disco cheio")))
    assert r.scan_portfolio("2026-08-31")["ativos"][0]["ticker"] == "NVDA"


def test_servico_recem_reiniciado_responde_o_ultimo_conhecido_NA_HORA(tmp_path, monkeypatch):
    """A abertura depois de um restart não espera varredura nenhuma.

    O tamanho importa: a watchlist real gera ~98 KB de JSON, e a pergunta é se ler
    isso do disco é "na hora" ou só "mais rápido que varrer". Aqui o arquivo é
    inflado até essa ordem de grandeza e a leitura é MEDIDA numa instância nova —
    a que um serviço recém-subido teria, sem memo nenhum.

    DENTE: o teto de 0,5s é folgadíssimo pra um ``json.load`` e impossível pra uma
    varredura (7s a 20s medidos no ar). Se alguém trocar a leitura por um caminho
    que varre, o teste cai — e o ``scan_watchlist`` explosivo diz na hora que foi isso.
    """
    import time as _t

    universo = [f"T{i:03d}" for i in range(60)]
    grande = _passada(universo)
    for a in grande["ativos"]:
        a["frames"] = a["frames"] * 12
    r1 = _runner(tmp_path)
    assert _reg(r1.scan_snapshot, grande, universo)
    assert r1.scan_snapshot.path.stat().st_size > 50_000, "o payload não ficou realista"

    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: pytest.fail("abrir não pode varrer"))
    novo = _runner(tmp_path)                     # o serviço acabou de subir
    assert novo._scan_memo is None
    t0 = _t.perf_counter()
    salvo = novo.scan_ultimo()
    dt = _t.perf_counter() - t0
    assert len(salvo["ativos"]) == 60
    assert dt < 0.5, f"a leitura do último conhecido levou {dt:.3f}s — não é 'na hora'"


# ---------------------------------------------------------------- o endpoint ----
@pytest.fixture
def servidor(tmp_path):
    from tradingagents.webui.server import make_server
    runner = _runner(tmp_path)
    httpd = make_server("127.0.0.1", 0, runner=runner)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", runner
    finally:
        httpd.shutdown()


def _get(url):
    import urllib.request
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def test_endpoint_sem_nada_salvo_devolve_vazio(servidor):
    base, _ = servidor
    assert _get(f"{base}/api/scan/salvo") == {}


def test_endpoint_devolve_o_ultimo_conhecido_sem_varrer(servidor, monkeypatch):
    base, runner = servidor
    _reg(runner.scan_snapshot, _passada(["AMD"]), ["AMD"])
    # se ele varresse, isto explodiria — a leitura é de ARQUIVO, custo zero
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: pytest.fail("o /salvo não pode varrer"))
    got = _get(f"{base}/api/scan/salvo")
    assert got["ativos"][0]["ticker"] == "AMD"
    assert got["gerado_em"] == _T1


def test_salvo_de_ontem_continua_sendo_servido_com_o_seu_carimbo(servidor):
    """Velho não se esconde nem se descarta: vai pra tela COM a data dele."""
    base, runner = servidor
    ontem = timeutil.stamp(datetime.now(timeutil.MANAUS) - timedelta(days=1))
    _reg(runner.scan_snapshot, _passada(["BTC-USD"], gerado_em=ontem), ["BTC-USD"])
    got = _get(f"{base}/api/scan/salvo")
    assert got["ativos"][0]["ticker"] == "BTC-USD"
    assert got["gerado_em"] == ontem
