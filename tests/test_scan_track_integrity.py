"""Integridade do track record do scan — o painel que vira NÚMERO a observação
"o 1-2-3 dá lucro em alguns dias".

Três defeitos independentes que se somavam (medidos no serviço no ar, 29/08, sobre
20 ativos × 3 frames):

1. **Alvo degenerado publicado** — 23 de 57 pares saíam com ``tp`` EXATAMENTE igual
   ao gatilho, todos carregando ``rr_note`` "o alvo já ficou para trás da entrada".
   Um alvo igual ao gatilho é ``bateu_tp`` no instante em que aciona: ACERTO
   FABRICADO. A raiz é aritmética — o alvo era filtrado no valor CRU (``h > entry``)
   e publicado ARREDONDADO, então um topo a menos de um centavo caía atrás da
   entrada. Caso real: MSFT 1d, gatilho 512,76 · TP 512,76 · SL 471,35.
2. **Imutabilidade com prazo de validade** — o fechamento era recalculado a cada
   leitura sobre as ÚLTIMAS N barras. A janela desliza: no 1h ela cobre poucas
   semanas, e o dia em que a barra do toque saísse dela um ``bateu_tp`` voltava
   calado pra "andamento".
3. **Métrica errada** — taxa de acerto sozinha, sobre uma amostra de R:R mediano
   0,13, onde o acerto de EQUILÍBRIO é 88,5%. Acerto alto com expectativa negativa
   é a armadilha clássica.
"""

import json

import pandas as pd
import pytest

import tradingagents.webui.scanner as sc
from tradingagents.dataflows.price_structure import (
    _nearest_overhead_high,
    _nearest_support_low,
    _risk_reward,
)
from tradingagents.webui.scanner import ScanLog, scan_symbol, scan_verdicts

pytestmark = pytest.mark.unit


# ------------------------------------------------- 1) o alvo degenerado --------
def _df(*precos):
    """DataFrame mínimo (Date/High/Low) pros seletores de nível."""
    return pd.DataFrame({
        "Date": pd.to_datetime([f"2026-08-{10 + i:02d}" for i in range(len(precos))]),
        "High": list(precos),
        "Low": list(precos),
    })


def test_topo_a_menos_de_um_centavo_da_entrada_nao_e_alvo():
    """O caso MSFT 1d, com os números reais do serviço no ar.

    Entrada (gatilho) 512,76 e um topo em 512,7649 — CRU está acima, PUBLICADO
    (2 casas) é o mesmo nível. O código antigo aceitava pelo cru e devolvia o
    arredondado: alvo 512,76 = entrada, retorno ZERO contra 41 pontos de risco.
    DENTE: com o filtro cru de volta, este teste devolve 512.76 e falha.
    """
    df = _df(512.7649, 515.0601)
    alvo = _nearest_overhead_high(df, [0, 1], 512.76)
    assert alvo is not None
    assert alvo["price"] > 512.76, alvo
    assert alvo["price"] == 515.06        # pula o topo indistinguível, pega o próximo


def test_sem_topo_distinguivel_o_alvo_e_None_honesto():
    """Não há alvo à frente: ``None`` (a tela diz "sem nível definido") em vez de
    um nível que empata com a entrada."""
    assert _nearest_overhead_high(_df(512.7649), [0], 512.76) is None


def test_fundo_a_menos_de_um_centavo_nao_vira_alvo_na_venda():
    """Espelho na venda: o arredondamento subia o fundo até a entrada."""
    assert _nearest_support_low(_df(463.2149), [0], 463.215) is None
    alvo = _nearest_support_low(_df(463.2149, 460.21), [0, 1], 463.22)
    assert alvo and alvo["price"] == 463.21


def test_risk_reward_mede_na_precisao_publicada():
    """Risco e retorno saem da MESMA precisão dos níveis exibidos — era daí que
    vinha um "retorno 0,0" convivendo com um alvo aparentemente acima."""
    rr = _risk_reward(512.7601, "gatilho", {"price": 471.35}, {"price": 515.06}, True)
    assert rr["entry"] == 512.76
    assert rr["reward"] == 2.3 and rr["risk"] == 41.41
    assert rr["rr"] == 0.06 and rr["note"] is None


def test_alvo_atras_da_entrada_ainda_e_recusado_com_motivo():
    """A rede de segurança continua: se algum caminho produzir um alvo atrás da
    entrada, ``rr`` é None COM motivo — nunca um número sem sentido."""
    rr = _risk_reward(512.76, "gatilho", {"price": 471.35}, {"price": 512.76}, True)
    assert rr["rr"] is None and "para trás" in rr["note"]


def _plan_com_alvo_incoerente():
    return {
        "price": 512.70, "setup_state": "ativo",
        "pattern": {"direction": "compra", "state": "formando", "trigger": 512.76},
        "stop": {"price": 471.35},
        "target": {"price": 512.76, "low": 507.86, "high": 517.66},
        "risk_reward": {"rr": None, "reward": 0.0, "risk": 41.41,
                        "note": "o alvo já ficou para trás da entrada — sem retorno a projetar."},
    }


@pytest.fixture
def _plans(monkeypatch):
    def install(mapa):
        monkeypatch.setattr(sc, "build_actionable_plan_dict",
                            lambda t, d, timeframe="1d", method="padrao": mapa.get((t, timeframe), {}))
        monkeypatch.setattr(sc, "build_price_chart",
                            lambda t, d, bars=260, timeframe="1d", method="padrao": {"candles": []})
        monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
    return install


def test_scan_nao_publica_alvo_quando_o_rr_traz_motivo(_plans):
    """DENTE do item 1: o scan publicava ``tp`` mesmo com o R:R recusado, e a tela
    mostrava "🎯 TP 512,76" ao lado de "🎯 gatilho 512,76 · R:R não calculável"."""
    _plans({("MSFT", "1d"): _plan_com_alvo_incoerente()})
    linha = scan_symbol("MSFT", "2026-08-29", frames=("1d",))["frames"][0]
    assert linha["estado"] == "em_gatilho"
    assert linha["tp"] is None, "alvo incoerente não se publica"
    assert linha["tp_faixa"] is None
    assert "para trás" in (linha["rr_note"] or ""), "o MOTIVO tem que viajar pro front"


def test_o_log_do_track_record_nao_recebe_alvo_incoerente(_plans, tmp_path):
    """Sem isto o alvo degenerado entra no ledger e vira acerto fabricado."""
    _plans({("MSFT", "1d"): _plan_com_alvo_incoerente()})
    linha = scan_symbol("MSFT", "2026-08-29", frames=("1d",))["frames"][0]
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({**linha, "ticker": "MSFT"})
    assert log.entries()[0]["tp"] is None


# -- entradas ANTIGAS: o ledger é append-only, a defesa é na leitura -------------
def _log_com(tmp_path, *entradas):
    path = tmp_path / "scans.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for e in entradas:
            fh.write(json.dumps(e) + "\n")
    return ScanLog(path)


def _serie(monkeypatch, candles, preco=None, capture=None):
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": preco, "pattern": None, "setup_state": "ativo"})

    def chart(t, d, bars=260, timeframe="1d", method="padrao"):
        if capture is not None:
            capture.append({"bars": bars, "timeframe": timeframe})
        return {"candles": candles}

    monkeypatch.setattr(sc, "build_price_chart", chart)
    monkeypatch.setattr(sc, "_live_price", lambda ticker: preco)


def _c(d, h, low):
    return {"d": d, "o": low, "h": h, "l": low, "c": low}


def test_alvo_incoerente_ja_logado_e_ignorado_na_leitura(tmp_path, monkeypatch):
    """ZEC-USD 4h de 29/08, entrada REAL do ledger: ``tp == trigger == 834,82``.

    O ledger é append-only — não se reescreve fato. Mas um alvo que não está à
    frente da entrada é ignorado na LEITURA, senão o trade fecha como ``bateu_tp``
    no instante em que aciona. Só o SL pode fechá-lo. DENTE: sem o guard, o
    veredito aqui é ``bateu_tp``.
    """
    log = _log_com(tmp_path, {
        "ts": "2026-08-29T14:48:36+00:00", "ticker": "ZEC-USD", "frame": "4h",
        "direction": "compra", "trigger": 834.82, "sl": 764.33, "tp": 834.82, "rr": None})
    _serie(monkeypatch,
           [_c("2026-08-29", 835.0, 830.0), _c("2026-08-30", 900.0, 834.9)],
           preco=890.0)
    v = scan_verdicts(log, "2026-08-31")["verdicts"][0]
    assert v["veredito"] != "bateu_tp", v
    assert v["tp_ignorado"] is True
    assert v["fechado"] is False


# ------------------------------------------ 2) a janela que desliza ------------
def test_fechado_persistido_sobrevive_a_janela_que_andou(tmp_path, monkeypatch):
    """O defeito com prazo de validade, reproduzido: o toque fecha hoje; amanhã a
    janela deslizou e a barra do toque não está mais na série.

    Antes, a segunda leitura devolvia "andamento" e a taxa de acerto mudava — o
    mesmo defeito (b)/(c) que o fechamento pela série dizia ter matado "por
    construção". Agora o fechamento é APENDADO no ledger e vale pra sempre.
    """
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1h",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    # 1ª leitura: a série cobre o gatilho e mostra o toque.
    _serie(monkeypatch, [_c("2026-08-20", 100.0, 99.0), _c("2026-08-21", 111.0, 100.0)],
           preco=101.0)
    primeira = scan_verdicts(log, "2026-08-22")
    assert primeira["verdicts"][0]["veredito"] == "bateu_tp"
    assert primeira["verdicts"][0]["fonte_veredito"] == "serie"
    assert primeira["taxa_acerto"] == 1.0

    # 2ª leitura, semanas depois: a janela andou — nem o toque nem o dia do log
    # estão mais na série (é exatamente o que acontece num frame de 1h).
    _serie(monkeypatch, [_c("2026-09-15", 98.0, 97.0)], preco=97.5)
    segunda = scan_verdicts(log, "2026-09-16")
    v = segunda["verdicts"][0]
    assert v["veredito"] == "bateu_tp", "a janela deslizou e apagou um acerto real"
    assert v["fonte_veredito"] == "ledger" and v["fechado_em"] == "2026-08-21"
    assert segunda["taxa_acerto"] == 1.0


def test_fechamento_e_gravado_uma_vez_so(tmp_path, monkeypatch):
    """Append-only não é append-repetido: o fato entra uma vez e a releitura o usa
    (sem nem buscar série — o ledger dispensa a conta)."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    chamadas = []
    _serie(monkeypatch, [_c("2026-08-20", 100.0, 99.0), _c("2026-08-21", 111.0, 100.0)],
           preco=101.0, capture=chamadas)
    scan_verdicts(log, "2026-08-22")
    n_depois_da_primeira = len(chamadas)
    scan_verdicts(log, "2026-08-23")
    scan_verdicts(log, "2026-08-24")
    assert len(chamadas) == n_depois_da_primeira, "releitura não precisa da série"
    linhas = [json.loads(x) for x in (tmp_path / "scans.jsonl").read_text().splitlines()]
    fechamentos = [x for x in linhas if x.get("tipo") == "fechamento"]
    assert len(fechamentos) == 1, fechamentos


def test_serie_que_nao_alcanca_o_gatilho_e_estado_proprio(tmp_path, monkeypatch):
    """``sem_serie_cobrindo`` ≠ ``andamento``: no primeiro não se sabe se tocou.

    Antes caía no ``else`` e era marcado a mercado como se estivesse aberto —
    afirmação sobre o que não se sabe, que é como uma taxa de acerto vira ficção.
    """
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1h",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    _serie(monkeypatch, [_c("2026-09-10", 101.0, 99.0)], preco=100.5)
    out = scan_verdicts(log, "2026-09-11")
    v = out["verdicts"][0]
    assert v["veredito"] == "sem_serie_cobrindo"
    assert v["fechado"] is False and v["motivo"]
    assert out["n_fechados"] == 0


def test_entrada_sem_carimbo_de_tempo_nao_e_avaliada(tmp_path, monkeypatch):
    """Sem ``ts`` não há janela: ``dia <= ""`` nunca é verdade, então TODA barra
    entrava — inclusive as anteriores ao gatilho, fabricando um toque que aconteceu
    antes de o setup existir. DENTE: sem a guarda, o veredito é ``bateu_tp``."""
    log = _log_com(tmp_path, {
        "ticker": "C", "frame": "1d", "direction": "compra",
        "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    _serie(monkeypatch, [_c("2020-01-02", 500.0, 400.0)], preco=101.0)
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["veredito"] == "sem_dado" and v["fechado"] is False
    assert "carimbo" in v["motivo"]


def test_a_janela_pedida_cobre_do_log_ate_a_data(tmp_path, monkeypatch):
    """O pedido de série deixa de ser o default de 260 barras: num 1h de 20 dias
    atrás, 260 barras não alcançam o gatilho (~11 dias de cripto)."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-01T12:00:00+00:00", "ticker": "C", "frame": "1h",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    chamadas = []
    _serie(monkeypatch, [_c("2026-08-01", 100.0, 99.0)], preco=100.0, capture=chamadas)
    scan_verdicts(log, "2026-08-31")
    assert chamadas and chamadas[0]["bars"] >= 30 * 24, chamadas
    assert chamadas[0]["bars"] <= sc._BARS_MAX


def test_frame_diario_nao_pede_janela_absurda(tmp_path, monkeypatch):
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0})
    chamadas = []
    _serie(monkeypatch, [_c("2026-08-20", 100.0, 99.0)], preco=100.0, capture=chamadas)
    scan_verdicts(log, "2026-08-28")
    assert chamadas[0]["bars"] == sc._BARS_MIN   # o mínimo já cobre 8 dias diários


# ---------------------------------------------- 3) expectativa, não só acerto --
def test_acerto_alto_com_expectativa_negativa_e_denunciado(tmp_path, monkeypatch):
    """A armadilha, com o R:R REAL da amostra de 29/08 (mediana 0,13).

    3 acertos em 4 = 75% de acerto — parece ótimo. Com R:R 0,13 a expectativa é
    NEGATIVA (−0,15R por trade) e o acerto de equilíbrio é 88,5%. Sem esta linha o
    painel diria "75% de acerto" sobre uma estratégia que perde dinheiro.
    """
    entradas = []
    for i, (tp_toca, ) in enumerate([(True,), (True,), (True,), (False,)]):
        entradas.append({
            "ts": "2026-08-20T12:00:00+00:00", "ticker": f"T{i}", "frame": "1d",
            "direction": "compra", "trigger": 100.0, "sl": 90.0,
            "tp": 101.3, "rr": 0.13, "_toca": tp_toca})
    log = _log_com(tmp_path, *entradas)
    candles = {f"T{i}": ([_c("2026-08-20", 100.0, 99.0),
                          _c("2026-08-21", 102.0, 99.5)] if e["_toca"] else
                         [_c("2026-08-20", 100.0, 99.0), _c("2026-08-21", 100.5, 89.0)])
               for i, e in enumerate(entradas)}
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": 100.0, "pattern": None, "setup_state": "ativo"})
    monkeypatch.setattr(sc, "build_price_chart",
                        lambda t, d, bars=260, timeframe="1d", method="padrao":
                        {"candles": candles.get(t, [])})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: 100.0)

    out = scan_verdicts(log, "2026-08-28")
    assert out["taxa_acerto"] == 0.75
    assert out["rr_medio"] == 0.13
    assert out["expectativa_r"] == pytest.approx(0.75 * 0.13 - 0.25, abs=1e-3)
    assert out["expectativa_r"] < 0, "75% de acerto com R:R 0,13 PERDE dinheiro"
    assert out["acerto_equilibrio"] == pytest.approx(0.885, abs=1e-3)
    assert out["n_com_rr"] == 4 and out["acerto_com_rr"] == 0.75


def test_expectativa_ausente_quando_nao_ha_rr_conhecido(tmp_path, monkeypatch):
    """Número inventado é pior que a ausência dele."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "C", "frame": "1d",
        "direction": "compra", "trigger": 100.0, "sl": 95.0, "tp": 110.0, "rr": None})
    _serie(monkeypatch, [_c("2026-08-20", 100.0, 99.0), _c("2026-08-21", 111.0, 100.0)],
           preco=101.0)
    out = scan_verdicts(log, "2026-08-28")
    assert out["taxa_acerto"] == 1.0
    assert out["expectativa_r"] is None and out["rr_medio"] is None
    assert out["n_com_rr"] == 0


def test_alvo_de_setup_ja_acionado_nao_e_rejeitado_por_engano(tmp_path, monkeypatch):
    """A contra-prova do guard: num setup ACIONADO a entrada é o PREÇO, não o
    gatilho, então um alvo entre os dois é legítimo — e o ``rr`` calculado prova.
    Rejeitá-lo trocaria o acerto fabricado por uma PERDA fabricada."""
    log = _log_com(tmp_path, {
        "ts": "2026-08-20T12:00:00+00:00", "ticker": "A", "frame": "1d",
        "direction": "compra", "pattern_state": "acionado",
        "trigger": 100.0, "sl": 95.0, "tp": 99.8, "rr": 0.4})
    _serie(monkeypatch, [_c("2026-08-20", 99.6, 99.4), _c("2026-08-21", 99.9, 99.5)],
           preco=99.9)
    v = scan_verdicts(log, "2026-08-28")["verdicts"][0]
    assert v["tp_ignorado"] is False
    assert v["veredito"] == "bateu_tp"


# ------------------------------- R:R residual: número certo, leitura errada ------
def _plan_msft_1h_acionado():
    """MSFT 1h de 29/08, os números REAIS do print: o padrão já acionou, o preço
    passou de 513,67 e o alvo é 513,73 — sobra 0,06 de retorno pra 28,70 de risco."""
    return {
        "price": 513.67, "setup_state": "ativo",
        "pattern": {"direction": "compra", "state": "acionado", "trigger": 497.14},
        "stop": {"price": 484.97},
        "target": {"price": 513.73},
        "risk_reward": {"rr": 0.0, "entry": 513.67, "risk": 28.70, "reward": 0.06,
                        "note": None, "entry_basis": "preço atual (padrão já acionado)"},
    }


def test_rr_zero_de_setup_acionado_e_marcado_como_residual(_plans):
    """O número está CERTO e a leitura estava errada: "R:R 0.00" lê-se "setup sem
    retorno", quando a verdade é "o trade já andou, sobrou quase nada". O flag é o
    que deixa a tela dizer isso com palavra em vez de repetir o número cru.

    DENTE: sem ``rr_residual``, a linha volta a publicar só o 0.0.
    """
    _plans({("MSFT", "1h"): _plan_msft_1h_acionado()})
    linha = scan_symbol("MSFT", "2026-08-29", frames=("1h",))["frames"][0]
    assert linha["estado"] == "em_movimento"     # acionado e preço além da entrada
    assert linha["rr"] == 0.0                    # o número NÃO muda
    assert linha["rr_residual"] is True
    assert linha["rr_retorno"] == 0.06 and linha["rr_risco"] == 28.70
    # e a BASE da entrada viaja junto: sem ela não dá pra saber que o R:R foi
    # medido do preço de agora, não do gatilho (do gatilho daria 1,36)
    assert "preço atual" in (linha["rr_basis"] or "")
    assert linha["rr_entry"] == 513.67


def test_setup_NAO_acionado_com_rr_baixo_nao_vira_residual(_plans):
    """Contra-prova: R:R ruim num setup que ainda NÃO acionou é R:R ruim mesmo — a
    entrada é o gatilho, o alvo não foi "praticamente alcançado", e trocar o número
    por texto ali esconderia um setup honestamente ruim."""
    plan = _plan_msft_1h_acionado()
    plan["pattern"]["state"] = "formando"
    plan["risk_reward"].update({"rr": 0.02, "entry": 497.14,
                                "entry_basis": "gatilho — rompimento da máxima do ponto 2"})
    _plans({("MSFT", "1h"): plan})
    linha = scan_symbol("MSFT", "2026-08-29", frames=("1h",))["frames"][0]
    assert linha["rr"] == 0.02
    assert linha["rr_residual"] is False


def test_acionado_com_retorno_de_verdade_nao_e_residual(_plans):
    """O outro lado: acionado com R:R 0,66 (BTC-USD 1h no mesmo print) continua
    mostrando o NÚMERO — o residual é pra quando quase não sobrou nada."""
    plan = _plan_msft_1h_acionado()
    plan["risk_reward"].update({"rr": 0.66, "reward": 18.9})
    _plans({("MSFT", "1h"): plan})
    linha = scan_symbol("MSFT", "2026-08-29", frames=("1h",))["frames"][0]
    assert linha["rr"] == 0.66 and linha["rr_residual"] is False


def test_o_limiar_do_residual_e_declarado():
    """Limiar arbitrário TEM que ser nomeado (mesma disciplina do _GATILHO_TOL)."""
    assert 0 < sc._RR_RESIDUAL <= 0.1
