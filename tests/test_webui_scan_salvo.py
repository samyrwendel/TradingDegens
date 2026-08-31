"""O último scan COMPLETO sobrevive ao processo (task 20260831-014).

O painel abria VAZIO os 8–20s da varredura porque nada no servidor guardava "o
último resultado". Havia três candidatos e nenhum servia: o ``_scan_memo`` do
runner (5s, em memória), o ``_live_cache`` do scanner (30s, em memória) e o
``scans.jsonl`` (em disco, mas append-only e só dos ``em_gatilho`` — não é uma
varredura, é um ledger de gatilhos).

Os dentes daqui, um por armadilha real:

* **restart não perde** — uma instância NOVA do store (e do runner) lê o que a
  anterior gravou; é o que separa disco de memória, e é a única coisa que o
  ``_scan_memo`` nunca poderia fazer.
* **a passada AGENDADA não sobrescreve** — ela varre um subconjunto (só o que o
  mercado justifica varrer agora). Gravá-la faria a abertura mostrar meia
  watchlist com cara de watchlist inteira: dado errado, silencioso, e pior que
  tela vazia.
* **vazio não vira "salvo"** — watchlist vazia devolve ``ativos: []``; guardar
  isso faria a abertura pintar uma lista vazia, que se lê como "não há nada em
  gatilho". Sem salvo, a tela cai no comportamento de primeira carga.
* **carimbo é do SERVIDOR** — sem ``gerado_em`` no payload, a tela só saberia a
  hora em que o JSON chegou nela, e um resultado lido do disco se passaria por
  recém-saído. É a mesma disciplina da "cotação de quando".
* **fail-open** — arquivo corrompido/ausente devolve ``{}``, nunca explode: uma
  leitura de conveniência não pode derrubar o painel.
"""

import json
import threading
from datetime import datetime, timedelta

import pytest

from tradingagents.webui import scanner, timeutil
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore, ScanSnapshotStore

pytestmark = pytest.mark.unit


def _resultado(ticker="MSFT", gerado_em="2026-08-31T14:32:00-04:00"):
    melhor = {"frame": "1d", "estado": "em_gatilho", "direction": "compra",
              "price": 513.53, "trigger": 512.76}
    return {"date": "2026-08-31", "frames": ["1d", "4h", "1h"],
            "resumo": {"em_gatilho": 1}, "gerado_em": gerado_em,
            "ativos": [{"ticker": ticker, "melhor": melhor, "frames": [melhor]}]}


# ------------------------------------------------------------------ o store ----
def test_salva_e_le_de_volta(tmp_path):
    ScanSnapshotStore(tmp_path).save(_resultado())
    got = ScanSnapshotStore(tmp_path).get()
    assert got["ativos"][0]["ticker"] == "MSFT"
    assert got["gerado_em"] == "2026-08-31T14:32:00-04:00"


def test_sem_nada_salvo_devolve_vazio_e_nao_explode(tmp_path):
    assert ScanSnapshotStore(tmp_path).get() == {}


def test_arquivo_corrompido_devolve_vazio(tmp_path):
    store = ScanSnapshotStore(tmp_path)
    store.save(_resultado())
    store.path.write_text("{isto não é json", encoding="utf-8")
    assert ScanSnapshotStore(tmp_path).get() == {}


def test_varredura_sem_ativos_nao_vira_salvo(tmp_path):
    store = ScanSnapshotStore(tmp_path)
    vazio = {"date": "2026-08-31", "frames": ["1d"], "resumo": {}, "ativos": [],
             "gerado_em": "2026-08-31T14:32:00-04:00"}
    assert store.save(vazio) is False
    assert store.get() == {}
    # e um vazio DEPOIS não pode apagar um resultado bom que já estava lá
    store.save(_resultado())
    assert store.save(vazio) is False
    assert store.get()["ativos"][0]["ticker"] == "MSFT"


def test_ultima_varredura_sobrescreve_a_anterior(tmp_path):
    store = ScanSnapshotStore(tmp_path)
    store.save(_resultado("MSFT"))
    store.save(_resultado("NVDA"))
    ativos = store.get()["ativos"]
    assert [a["ticker"] for a in ativos] == ["NVDA"]   # o estado, não um histórico


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


def test_scan_portfolio_salva_e_um_processo_NOVO_enxerga(tmp_path, monkeypatch):
    """O dente do restart: quem grava e quem lê são instâncias diferentes."""
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _resultado("NVDA"))
    r1 = _runner(tmp_path)
    r1.watchlist_store.set(["NVDA"])
    assert r1.scan_ultimo() == {}, "nada salvo ainda"
    r1.scan_portfolio("2026-08-31")

    r2 = _runner(tmp_path)          # como se o serviço tivesse reiniciado
    salvo = r2.scan_ultimo()
    assert salvo["ativos"][0]["ticker"] == "NVDA"
    assert salvo["gerado_em"] == "2026-08-31T14:32:00-04:00"


def test_scan_agendado_nao_sobrescreve_o_scan_completo(tmp_path, monkeypatch):
    """A passada agendada varre um SUBCONJUNTO — não pode virar "o último scan"."""
    completo = _resultado("MSFT")
    completo["ativos"].append({"ticker": "NVDA", "melhor": {"estado": "formando"},
                               "frames": []})
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: completo)
    r = _runner(tmp_path)
    r.watchlist_store.set(["MSFT", "NVDA"])
    r.scan_portfolio("2026-08-31")
    assert len(r.scan_ultimo()["ativos"]) == 2

    # agora a agenda roda com um alvo só (cripto fora do pregão, p.ex.)
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _resultado("MSFT"))
    monkeypatch.setattr("tradingagents.webui.agenda.sessao_de_mercado",
                        lambda w, f: ("fechada", None))
    monkeypatch.setattr("tradingagents.webui.agenda.alvos_da_passada",
                        lambda w, s: ["MSFT"])
    r.scan_agendado()
    assert len(r.scan_ultimo()["ativos"]) == 2, "a passada parcial virou o último scan"


def test_falha_de_disco_nao_derruba_a_varredura(tmp_path, monkeypatch):
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: _resultado("NVDA"))
    r = _runner(tmp_path)
    r.watchlist_store.set(["NVDA"])
    monkeypatch.setattr(r.scan_snapshot, "save",
                        lambda res: (_ for _ in ()).throw(OSError("disco cheio")))
    assert r.scan_portfolio("2026-08-31")["ativos"][0]["ticker"] == "NVDA"


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


def test_endpoint_devolve_o_salvo_sem_varrer(servidor, monkeypatch):
    base, runner = servidor
    runner.scan_snapshot.save(_resultado("AMD"))
    # se ele varresse, isto explodiria — a leitura é de ARQUIVO, custo zero
    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: pytest.fail("o /salvo não pode varrer"))
    got = _get(f"{base}/api/scan/salvo")
    assert got["ativos"][0]["ticker"] == "AMD"
    assert got["gerado_em"] == "2026-08-31T14:32:00-04:00"


def test_servico_recem_reiniciado_responde_o_salvo_NA_HORA(tmp_path, monkeypatch):
    """A abertura depois de um restart não espera varredura nenhuma.

    O tamanho importa: a watchlist real gera ~98 KB de JSON, e a pergunta é se
    ler isso do disco é "na hora" ou só "mais rápido que varrer". Aqui o
    snapshot é inflado até essa ordem de grandeza e a leitura é MEDIDA numa
    instância nova — a que um serviço recém-subido teria, sem memo nenhum.

    DENTE: o teto de 0,5s é folgadíssimo pra um ``json.load`` e impossível pra
    uma varredura (7s a 20s medidos no ar). Se alguém trocar a leitura por um
    caminho que varre, o teste cai — e o ``scan_watchlist`` explosivo abaixo
    diz na hora que foi isso.
    """
    import time as _t

    grande = _resultado("MSFT")
    frames = grande["ativos"][0]["frames"]
    grande["ativos"] = [{"ticker": f"T{i:03d}", "melhor": frames[0],
                         "frames": frames * 12} for i in range(60)]
    r1 = _runner(tmp_path)
    assert r1.scan_snapshot.save(grande) is True
    assert r1.scan_snapshot.path.stat().st_size > 50_000, "o payload não ficou realista"

    monkeypatch.setattr("tradingagents.webui.runner.scan_watchlist",
                        lambda tickers, date: pytest.fail("abrir não pode varrer"))
    novo = _runner(tmp_path)                     # o serviço acabou de subir
    assert novo._scan_memo is None
    t0 = _t.perf_counter()
    salvo = novo.scan_ultimo()
    dt = _t.perf_counter() - t0
    assert len(salvo["ativos"]) == 60
    assert dt < 0.5, f"a leitura do salvo levou {dt:.3f}s — não é 'na hora'"


def test_salvo_de_ontem_continua_sendo_servido_com_o_seu_carimbo(servidor):
    """Velho não se esconde nem se descarta: vai pra tela COM a data dele."""
    base, runner = servidor
    ontem = timeutil.stamp(datetime.now(timeutil.MANAUS) - timedelta(days=1))
    runner.scan_snapshot.save(_resultado("BTC-USD", gerado_em=ontem))
    got = _get(f"{base}/api/scan/salvo")
    assert got["ativos"][0]["ticker"] == "BTC-USD"
    assert got["gerado_em"] == ontem
