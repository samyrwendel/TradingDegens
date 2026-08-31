"""A ESCADA — a mesma leitura estrutural nos CINCO frames, na análise inicial.

*"Preciso que a análise do Storm123 e Setup123 seja mais ampla e na análise inicial
já faça os timeframes de 15m, 1h, 4h, D e S."* (task 20260831-012)

O que este arquivo trava, e por quê:

1. **Os cinco frames vêm de uma passada só, na ordem da escada** (maior → menor).
   A ordem não é estética: no método o frame MAIOR manda na TESE e o menor no
   TIMING, então uma escada embaralhada mente sobre a hierarquia da leitura.
2. **Em paralelo, com UMA cotação.** É I/O-bound; em série os cinco somam a
   latência de todos (medido: ~5,6s em série × ~2,6s em paralelo, símbolo frio).
   E a cotação é do ATIVO, não do frame — buscá-la cinco vezes seria pagar cinco
   vezes pelo mesmo número.
3. **Frame que o ativo não opera não entra**, e a lista sai da MESMA fonte de
   verdade do seletor e do ``/api/chart`` (``timeframes_for_asset``) — duas listas
   mantidas à mão foi como o seletor e o backend já discordaram antes.
4. **Frame sem candle declara ausência.** ``sem_dado`` com motivo, nunca um nível
   inventado — a regra de sempre, aqui multiplicada por cinco.
5. **A escada nunca derruba a análise.** É enriquecimento: qualquer erro vira ``{}``
   e a run estrutural entrega o que sempre entregou.
6. **A escada não elege frame nenhum.** Quem tem veredito é a run; ``scan_symbol``
   devolve um ``melhor`` (urgência, pra ordenar a watchlist) e trazê-lo pra cá
   criaria um segundo veredito na mesma tela.
"""

import time

from tradingagents.webui import runner as rn, scanner as sc


# ------------------------------------------------------------ scan_symbol_frames
def _row(ticker, date, frame, live_price=None):
    return {"frame": frame, "estado": "formando", "price": live_price, "trigger": 10.0}


def test_a_escada_sai_do_frame_maior_pro_menor_com_os_cinco(monkeypatch):
    monkeypatch.setattr(sc, "_frame_row", _row)
    monkeypatch.setattr(sc, "_live_price", lambda t, **k: 123.0)
    out = sc.scan_symbol_frames("btc-usd", "2026-08-31")
    assert [f["frame"] for f in out["frames"]] == ["1w", "1d", "4h", "1h", "15m"]
    assert out["ticker"] == "BTC-USD", "o símbolo é normalizado como no resto do scan"
    # A escada NÃO elege: um "melhor" aqui seria um segundo veredito na mesma tela.
    assert "melhor" not in out


def test_a_cotacao_e_do_ATIVO_e_se_busca_uma_vez_so(monkeypatch):
    """Cinco frames, uma cotação. Ela não muda com o timeframe, e o ``_live_price``
    é a chamada que o scan já mediu como cara (~0,9s por ativo)."""
    chamadas = []
    monkeypatch.setattr(sc, "_frame_row", _row)
    monkeypatch.setattr(sc, "_live_price",
                        lambda t, **k: (chamadas.append(t), 99.0)[1])
    out = sc.scan_symbol_frames("ETH-USD", "2026-08-31")
    assert len(chamadas) == 1, chamadas
    assert all(f["price"] == 99.0 for f in out["frames"]), out


def test_os_frames_rodam_CONCORRENTES_nao_em_fila(monkeypatch):
    """A prova é de TEMPO, não de estrutura: cinco leituras de 120ms em fila são
    ≥600ms; concorrentes cabem numa janela bem menor. Sem isto a escada custaria a
    soma dos cinco frames, que é o único jeito de ela ficar cara."""
    monkeypatch.setattr(sc, "_live_price", lambda t, **k: 1.0)

    def lento(ticker, date, frame, live_price=None):
        time.sleep(0.12)
        return {"frame": frame, "estado": "formando"}

    monkeypatch.setattr(sc, "_frame_row", lento)
    t0 = time.perf_counter()
    sc.scan_symbol_frames("X", "2026-08-31")
    gasto = time.perf_counter() - t0
    assert gasto < 0.40, f"cinco frames em fila custariam ~0,60s; gastou {gasto:.2f}s"


def test_frame_sem_candle_vira_linha_declarada_e_nao_some(monkeypatch):
    """Ausência é informação: a linha FICA, com o estado e o motivo. Sumir com o
    frame faria a escada mostrar quatro degraus e nada explicando o quinto."""
    def sem_dado(ticker, date, frame, live_price=None):
        if frame == "15m":
            return {"frame": frame, "estado": "sem_dado", "motivo": "fonte: intradiario_indisponivel"}
        return {"frame": frame, "estado": "formando", "trigger": 5.0}

    monkeypatch.setattr(sc, "_frame_row", sem_dado)
    monkeypatch.setattr(sc, "_live_price", lambda t, **k: 1.0)
    frames = sc.scan_symbol_frames("AAPL", "2026-08-31")["frames"]
    assert len(frames) == 5
    ausente = frames[-1]
    assert ausente["frame"] == "15m" and ausente["estado"] == "sem_dado"
    assert ausente["motivo"], "ausência sem motivo é ausência escondida"
    assert "trigger" not in ausente, "frame sem candle não publica nível"


def test_escada_sem_frames_nao_estoura(monkeypatch):
    monkeypatch.setattr(sc, "_live_price", lambda t, **k: 1.0)
    assert sc.scan_symbol_frames("X", "2026-08-31", frames=())["frames"] == []
    assert sc.scan_symbol_frames("", "2026-08-31")["frames"] == []


# ------------------------------------------------------------ leitura_multiframe
def test_a_escada_so_pede_frame_que_o_ATIVO_opera(monkeypatch):
    """A lista de frames sai de ``timeframes_for_asset`` — a mesma que o seletor e o
    ``/api/chart`` leem. Uma lista própria aqui é como duas superfícies passam a
    discordar sobre o que existe."""
    pedidos = {}

    def falso(ticker, date, frames=None, workers=None):
        pedidos["frames"] = frames
        return {"ticker": ticker, "frames": [{"frame": f} for f in frames]}

    monkeypatch.setattr(sc, "scan_symbol_frames", falso)
    monkeypatch.setattr(rn, "timeframes_for_asset", lambda a: ["1d", "4h"])
    mf = rn.leitura_multiframe("X", "2026-08-31", "stock", "setup123", "1d")
    assert pedidos["frames"] == ("1d", "4h")
    assert [f["frame"] for f in mf["frames"]] == ["1d", "4h"]


def test_o_veredito_viaja_no_payload_e_nao_muda_leitura_nenhuma(monkeypatch):
    """O frame do veredito é um CARIMBO: diz qual das cinco decidiu. Se ele mudasse
    a leitura, a escada teria duas versões da mesma linha — uma "oficial" e uma não."""
    monkeypatch.setattr(
        sc, "scan_symbol_frames",
        lambda t, d, frames=None, workers=None: {
            "ticker": t, "frames": [{"frame": f, "estado": "formando"} for f in frames]})
    a = rn.leitura_multiframe("X", "2026-08-31", "crypto", "storm123", "4h")
    b = rn.leitura_multiframe("X", "2026-08-31", "crypto", "storm123", "1d")
    assert a["veredito"] == "4h" and b["veredito"] == "1d"
    assert a["metodo"] == "storm123"
    assert a["frames"] == b["frames"], "o carimbo não pode reescrever a leitura"
    assert a["ms"] >= 0, "o custo medido viaja no payload — a decisão é de quem o vê"


def test_a_escada_falhando_NAO_derruba_a_analise(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("fonte fora do ar")

    monkeypatch.setattr(sc, "scan_symbol_frames", explode)
    assert rn.leitura_multiframe("X", "2026-08-31", "crypto", "setup123", "1d") == {}


# ------------------------------------------------- a escada dentro da run ------
def _runner_estrutural(tmp_path, monkeypatch):
    from tradingagents.webui.store import HistoryStore

    monkeypatch.setattr(rn, "fetch_price_chart", lambda *a, **k: {"candles": [{"c": 1}]})
    monkeypatch.setattr(rn, "plano_com_storm",
                        lambda *a, **k: {"price": 1.0, "setup_state": "ativo"})
    monkeypatch.setattr(rn.AnalysisRunner, "detect_asset_type", staticmethod(lambda t: "crypto"))
    return rn.AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                             store=HistoryStore(tmp_path))


def _espera(runner, run_id, limite=8.0):
    fim = time.time() + limite
    while time.time() < fim:
        snap = runner.status(run_id)
        if snap.get("status") in ("done", "error"):
            return snap
        time.sleep(0.02)
    raise AssertionError("run estrutural não terminou")


def test_a_run_ESTRUTURAL_ja_nasce_com_a_escada_no_resultado(tmp_path, monkeypatch):
    """O pedido em uma linha: a análise INICIAL já entrega os cinco. Antes disto a
    run trazia UM frame e o resto era trocar de chip cinco vezes."""
    r = _runner_estrutural(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sc, "scan_symbol_frames",
        lambda t, d, frames=None, workers=None: {
            "ticker": t, "frames": [{"frame": f, "estado": "formando"} for f in frames]})
    rid = r.start("BTC-USD", "2026-08-31", method="storm123", timeframe="4h", reuse=False)
    snap = _espera(r, rid)
    mf = snap["result"]["multiframe"]
    assert [f["frame"] for f in mf["frames"]] == ["1w", "1d", "4h", "1h", "15m"]
    assert mf["veredito"] == "4h", "o carimbo é o frame em que a run rodou"
    assert mf["metodo"] == "storm123"


def test_a_escada_quebrada_deixa_a_run_estrutural_INTEIRA(tmp_path, monkeypatch):
    """Enriquecimento não pode virar dependência: sem a escada a análise continua
    entregando chart, plano e estado como sempre entregou."""
    r = _runner_estrutural(tmp_path, monkeypatch)

    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sc, "scan_symbol_frames", explode)
    rid = r.start("BTC-USD", "2026-08-31", method="setup123", timeframe="1d", reuse=False)
    snap = _espera(r, rid)
    assert snap["status"] == "done"
    assert snap["result"]["multiframe"] == {}
    assert snap["result"]["actionable"]["setup_state"] == "ativo"
    assert snap["result"]["setup123"] is True


def test_a_escada_sobrevive_ao_HISTORICO(tmp_path, monkeypatch):
    """A run é persistida inteira, então reabrir uma análise de ontem tem que trazer
    a escada daquele dia — e não recomputá-la com o preço de hoje, que descreveria
    outra tela. O front lê ``result.multiframe`` do mesmo jeito nos dois caminhos."""
    r = _runner_estrutural(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sc, "scan_symbol_frames",
        lambda t, d, frames=None, workers=None: {
            "ticker": t, "frames": [{"frame": f, "estado": "formando"} for f in frames]})
    rid = r.start("BTC-USD", "2026-08-31", method="setup123", timeframe="1d", reuse=False)
    _espera(r, rid)
    registro = r.store.get(rid)
    frames = registro["result"]["multiframe"]["frames"]
    assert [f["frame"] for f in frames] == ["1w", "1d", "4h", "1h", "15m"]
