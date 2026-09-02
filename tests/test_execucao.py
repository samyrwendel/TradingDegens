"""A camada de EXECUÇÃO: o que fazer com os níveis (task 20260830-012).

Pedido do Samyr: *"quero um card explicando as entradas alvos como inserir as ordens e
onde colocar SL, TPS e onde invalida, e se ainda vale a pena entrar, ou se é pra
aguardar recuo até faixa tal"* + *"um índice de confiabilidade comparando com ordens
anteriores que bateram os TPs, SL e se devemos proteger com BE e um trailing stop que
pode ser habilitado"*.

Os casos abaixo são os **casos de aceitação da spec** do degenbot
(``~/brain/trading-ops/erick-camada-de-execucao-e-saida-spec.md`` §11), traduzidos em
teste. Dois invariantes atravessam tudo:

  * **nada de nível inventado** — todo preço do card sai do plano que o painel já
    desenha; o card acrescenta a POLÍTICA, não números;
  * **o que é `sem evidência` continua declarado** — a fração exata de cada alvo, o
    break-even como regra do Erick e o ATR como régua de trailing NÃO estão no corpus,
    e o card diz isso em vez de fabricar autoridade.
"""

import pytest

from tradingagents.webui import execucao as ex
from tradingagents.webui.scanner import _GATILHO_TOL, _RR_RESIDUAL


def _plano(**over):
    """O CRWD do print: compra, preço colado no gatilho p3."""
    base = {
        "price": 218.40, "setup_state": "aguardar_rompimento",
        "pattern": {"trigger": 218.56, "state": "formando", "direction": "compra"},
        "invalidation": {"price": 181.32, "meaning": "perde o ponto 3"},
        "stop": {"price": 210.53, "basis": "invalidação + folga de 0.5·ATR14"},
        "target": {"price": 237.11, "label": "topo anterior 2026-07-02"},
        "realize_zone": {"price": 219.35, "role": "alvo",
                         "role_label": "realização parcial", "label": "resistência"},
        "buy_zone": {"price": 211.27, "ma_label": "MMS20"},
        "risk_reward": {"entry": 218.56, "entry_basis": "gatilho", "risk": 8.03,
                        "reward": 18.55, "rr": 2.31, "note": None},
    }
    base.update(over)
    return base


# ───────────────── §7 — "ainda vale a pena entrar?" (caso 11.1) ─────────────────
@pytest.mark.unit
def test_preco_no_gatilho_com_rr_que_paga_e_ENTRAR():
    """O caso do print: preço 218,40 contra gatilho 218,56 → 0,07% de distância."""
    v = ex.veredito_entrada(_plano())
    assert v["estado"] == "entrar", v
    assert v["nivel"] == 218.56, v
    assert "no ponto de entrada" in v["motivo"], v["motivo"]
    assert v["dist_pct"] <= _GATILHO_TOL, v


@pytest.mark.unit
def test_retorno_residual_e_PASSAR_com_o_motivo_escrito():
    """Caso 11.1 da spec, com os números reais do MSFT 1h de 29/08: gatilho 497,14,
    alvo 513,73, preço 513,67 — do gatilho o R:R era 1,36; de agora é ~0."""
    p = _plano(price=513.67,
               pattern={"trigger": 497.14, "state": "acionado", "direction": "compra"},
               stop={"price": 484.97, "basis": "invalidação + folga"},
               target={"price": 513.73, "label": "topo anterior"},
               realize_zone=None,
               risk_reward={"entry": 513.67, "risk": 28.70, "reward": 0.06, "rr": 0.0,
                            "note": None,
                            "no_gatilho": {"entry": 497.14, "risk": 12.17,
                                           "reward": 16.59, "rr": 1.36},
                            "andado_pct": 99.6, "sobra_pct": 0.4})
    v = ex.veredito_entrada(p)
    assert v["estado"] == "passar", v
    assert "residual" in v["motivo"], v["motivo"]
    assert "99%" in v["motivo"] or "100%" in v["motivo"], v["motivo"]
    # GUARDA da spec: o R:R do GATILHO não pode ser exibido como se fosse o de agora
    assert v["rr_agora"] == 0.0 and v["rr_gatilho"] == 1.36, v


@pytest.mark.unit
def test_setup_vivo_mas_esticado_e_AGUARDAR_RECUO_ate_um_nivel_QUE_JA_EXISTE():
    """Caso 11.1, segundo bloco. O <nível> não é inventado: é a faixa da média que o
    painel já imprime — a "faixa X" que o Samyr pediu."""
    p = _plano(price=230.0,
               pattern={"trigger": 218.56, "state": "acionado", "direction": "compra"},
               risk_reward={"entry": 230.0, "risk": 19.47, "reward": 7.11, "rr": 0.37,
                            "note": None,
                            "no_gatilho": {"entry": 218.56, "risk": 8.03,
                                           "reward": 18.55, "rr": 2.31},
                            "andado_pct": 61.6, "sobra_pct": 38.4})
    v = ex.veredito_entrada(p)
    assert v["estado"] == "aguardar", v
    assert v["nivel"] == 211.27, ("o nível é a buy_zone do plano, não um preço novo", v)
    assert "MMS20" in v["rotulo"], v
    # o número sai em pt-BR: o card não pode ser a única superfície com ponto decimal
    assert "ESTICADO" in v["motivo"] and "2,31:1" in v["motivo"], v["motivo"]
    assert "0,37:1" in v["motivo"], v["motivo"]
    mostra_a_queda = "caiu de" in v["motivo"] and "para" in v["motivo"]
    assert mostra_a_queda, ("a frase mostra a QUEDA do R:R, que é a razão", v["motivo"])


@pytest.mark.unit
def test_invalidado_e_PASSAR_antes_de_qualquer_outra_conta():
    """Setup morto não tem veredito de entrada — e a invalidação vem ANTES do R:R:
    um plano invalidado com R:R aritmeticamente bonito continua morto."""
    p = _plano(price=170.0)
    v = ex.veredito_entrada(p)
    assert v["estado"] == "passar", v
    assert "invalidação" in v["motivo"], v["motivo"]


@pytest.mark.unit
def test_sem_padrao_nao_ha_veredito_de_entrada():
    v = ex.veredito_entrada({"price": 100.0})
    assert v["estado"] == "sem_setup", v
    assert ex.ordens({"price": 100.0}) == []


@pytest.mark.unit
def test_venda_usa_o_seu_proprio_lado_da_estrutura():
    """Espelho, não herança do long: na venda invalida ACIMA e as ordens invertem."""
    p = _plano(price=100.0, setup_state="ativo",
               pattern={"trigger": 100.0, "state": "formando", "direction": "venda"},
               invalidation={"price": 110.0, "meaning": "retomada do ponto 3"},
               stop={"price": 112.0, "basis": "invalidação + folga"},
               target={"price": 80.0, "label": "fundo anterior"},
               realize_zone=None, buy_zone=None,
               risk_reward={"entry": 100.0, "risk": 12.0, "reward": 20.0, "rr": 1.67,
                            "note": None})
    v = ex.veredito_entrada(p)
    assert v["estado"] == "entrar", v
    o = ex.ordens(p, v)
    assert o[0]["lado"] == "venda" and o[1]["lado"] == "compra", o
    # e um preço ACIMA da invalidação mata o setup de venda
    morto = ex.veredito_entrada({**p, "price": 115.0})
    assert morto["estado"] == "passar", morto


# ──────────────────── §1 — as ordens, na ordem de digitar ───────────────────────
@pytest.mark.unit
def test_as_ordens_saem_na_sequencia_e_a_entrada_e_sempre_a_LIMITE():
    """A regra dura do método: ordem a limite no recuo, NUNCA mercado no esticado
    ("quem comprar aqui vai sentar na graxa")."""
    p = _plano()
    o = ex.ordens(p)
    assert [x["passo"] for x in o] == [1, 2, 3, 4], o
    assert o[0]["papel"] == "entrada" and o[0]["tipo"] == "limite", o[0]
    assert o[1]["papel"] == "stop (SL)" and o[1]["tipo"] == "stop", o[1]
    assert o[1]["price"] == 210.53 and "ATR" in o[1]["base"], o[1]
    assert o[2]["papel"].startswith("T1") and o[3]["papel"].startswith("T2"), o
    # NENHUM preço inventado: todos saem do plano
    do_plano = {218.56, 210.53, 219.35, 237.11}
    assert {x["price"] for x in o} == do_plano, o


@pytest.mark.unit
def test_no_aguardar_a_ordem_de_entrada_e_no_RECUO_e_nao_no_gatilho():
    """É a diferença que responde a pergunta dele: onde eu ponho a ordem AGORA."""
    p = _plano(price=230.0,
               pattern={"trigger": 218.56, "state": "acionado", "direction": "compra"},
               risk_reward={"entry": 230.0, "risk": 19.47, "reward": 7.11, "rr": 0.37,
                            "note": None,
                            "no_gatilho": {"entry": 218.56, "risk": 8.03,
                                           "reward": 18.55, "rr": 2.31},
                            "andado_pct": 61.6, "sobra_pct": 38.4})
    o = ex.ordens(p)
    assert o[0]["price"] == 211.27, ("a entrada vai pra faixa de recuo", o[0])
    assert "recuo" in o[0]["base"], o[0]
    assert "nunca a mercado" in o[0]["base"], ("a regra dura fica escrita", o[0])


@pytest.mark.unit
def test_passar_nao_emite_ordem_de_entrada():
    """Um card que diz PASSAR e mesmo assim imprime onde comprar é um card que se
    contradiz — e o número é o que fica na cabeça de quem lê."""
    p = _plano(price=170.0)
    o = ex.ordens(p)
    assert not any(x["papel"] == "entrada" for x in o), o


# ─────────────── §3 — saída fracionada: estrutura sim, % inventado não ──────────
@pytest.mark.unit
def test_a_saida_e_estrutural_e_a_fracao_fica_declarada_a_calibrar():
    """O corpus dá UM caso com número (BTC 19/08, 95/5, N=1) e o resto qualitativo.
    A ESTRUTURA (grosso + resíduo) é sustentada; a porcentagem NÃO — e um "70/30" com
    cara de regra seria exatamente a invenção que a governança proíbe."""
    s = ex.saida(_plano())
    assert s["forma"] == "grosso_e_residuo", s
    assert [a["fracao"] for a in s["alvos"]] == ["grosso", "resíduo"], s["alvos"]
    assert "a calibrar" in s["calibrar"], s
    assert "%" not in s["texto"], ("nada de percentual cravado no texto", s["texto"])


@pytest.mark.unit
def test_sem_alvo_estrutural_a_saida_e_por_EXAUSTAO_e_diz_isso():
    p = _plano(target=None, realize_zone=None)
    s = ex.saida(p)
    assert s["forma"] == "por_exaustao", s
    assert "EXAUSTÃO" in s["texto"], s["texto"]


# ───────────────────── §4/§5 — BE e trailing são OPT-IN ────────────────────────
@pytest.mark.unit
def test_be_e_trailing_nascem_DESLIGADOS_com_o_porque_escrito():
    """Caso 11.5. E não é preguiça de default: o método COMPRA o recuo à média, então
    um BE/trailing ligado ejetaria o trade no pullback em que ele adicionaria."""
    p = ex.protecao()
    assert p["be"]["ligado"] is False and p["trailing"]["ligado"] is False, p
    assert "recuo à média é ENTRADA" in p["be"]["nota"], p["be"]
    assert "sem evidência" in p["be"]["evidencia"], ("o BE não é regra observada do "
                                                     "Erick, e o card diz isso", p["be"])
    assert "+1R" in " ".join(g["texto"] for g in p["be"]["gatilhos"]), p["be"]
    assert "fundo ascendente" in " ".join(g["texto"] for g in p["be"]["gatilhos"]), p["be"]


@pytest.mark.unit
def test_o_trailing_segue_media_e_fundo_e_NAO_ATR():
    """O Samyr perguntou "média? fundo? ATR?". O corpus responde: média ascendente e
    fundo ascendente. O ATR é utilitário do motor (folga do stop) — chamá-lo de régua
    de trailing do Erick seria inventar."""
    t = ex.protecao()["trailing"]
    assert "EMA 21" in t["referencia"] and "FUNDO ascendente" in t["referencia"], t
    assert "ATR" in t["evidencia"] and "sem evidência" in t["evidencia"], t
    assert "FECHAMENTO" in t["disparo"] and "pavio não dispara" in t["disparo"], t
    assert "RESÍDUO" in t["nota"], ("liga no resíduo, não na posição inteira", t)


# ─────────── §8 — índice de confiabilidade: o GATE DE N (caso 11.2) ────────────
@pytest.mark.unit
def test_com_3_fechados_NAO_aparece_taxa_de_acerto():
    """O cerne do pedido. Taxa de acerto com 3 casos é ruído e enganaria mais que
    ajudaria — a tela DIZ que não há amostra em vez de exibir um número."""
    c = ex.confiabilidade({"123": {"n": 9, "n_fechados": 3, "taxa_acerto": 0.6667,
                                   "expectativa_r": 0.2}})
    s = c["setups"]["123"]
    assert s["nivel"] == "insuficiente", s
    assert s["taxa_acerto"] is None and s["ic95"] is None, s
    assert "amostra insuficiente (n=3)" in s["texto"], s["texto"]


@pytest.mark.unit
def test_entre_5_e_20_a_taxa_sai_SEMPRE_com_o_intervalo():
    c = ex.confiabilidade({"123": {"n": 20, "n_fechados": 12, "taxa_acerto": 0.5,
                                   "expectativa_r": -0.1, "rr_medio": 0.8,
                                   "acerto_equilibrio": 0.5556, "n_com_rr": 12}})
    s = c["setups"]["123"]
    assert s["nivel"] == "preliminar", s
    assert s["taxa_acerto"] == 0.5 and s["ic95"] is not None, s
    lo, hi = s["ic95"]
    assert hi - lo > 0.4, ("com n=12 o intervalo ainda é largo, e é por isso que ele "
                           "precisa aparecer", s["ic95"])
    assert "intervalo largo" in s["texto"], s["texto"]


@pytest.mark.unit
def test_com_20_ou_mais_a_taxa_vira_numero_de_trabalho():
    c = ex.confiabilidade({"storm": {"n": 40, "n_fechados": 25, "taxa_acerto": 0.6,
                                     "expectativa_r": 0.35, "rr_medio": 1.2,
                                     "acerto_equilibrio": 0.4545, "n_com_rr": 25}})
    s = c["setups"]["storm"]
    assert s["nivel"] == "operavel", s
    assert s["ic95"] is not None and s["taxa_acerto"] == 0.6, s


@pytest.mark.unit
def test_a_expectativa_vem_SEMPRE_que_houver_base_mesmo_sem_taxa():
    """"70% de acerto com R:R 0,13 perde dinheiro" — o índice lidera pela expectativa,
    e ela não fica refém do gate da taxa."""
    c = ex.confiabilidade({"123": {"n": 5, "n_fechados": 3, "taxa_acerto": 1.0,
                                   "expectativa_r": -0.42, "rr_medio": 0.13,
                                   "acerto_equilibrio": 0.885, "n_com_rr": 3}})
    s = c["setups"]["123"]
    assert s["taxa_acerto"] is None, ("nada de 'confiabilidade 100%' com n=3", s)
    assert s["expectativa_r"] == -0.42, s
    assert s["acerto_equilibrio"] == 0.885, s


@pytest.mark.unit
def test_o_indice_e_POR_SETUP_e_nunca_somado():
    """Lição da task 008: acerto de um grupo com R:R de outro não descreve trade
    nenhum. Os dois setups têm stop, alvo e R:R construídos por regras diferentes."""
    c = ex.confiabilidade({"123": {"n": 30, "n_fechados": 22, "taxa_acerto": 0.5},
                           "storm": {"n": 8, "n_fechados": 2, "taxa_acerto": 1.0}})
    assert set(c["setups"]) == {"123", "storm"}, c
    assert c["setups"]["123"]["nivel"] == "operavel", c
    assert c["setups"]["storm"]["nivel"] == "insuficiente", c
    assert c["setups"]["storm"]["taxa_acerto"] is None, (
        "2 fechados não viram 'acerto de 100%'", c)


@pytest.mark.unit
def test_wilson_aperta_conforme_a_amostra_cresce():
    """A justificativa dos cortes 5/20, medida: o intervalo é o que desmente a taxa."""
    larguras = []
    for n in (4, 12, 40, 200):
        lo, hi = ex.wilson(n // 2, n)
        larguras.append(round(hi - lo, 3))
    assert larguras == sorted(larguras, reverse=True), larguras
    assert larguras[0] > 0.6, ("com n=4 o intervalo cobre quase tudo", larguras)
    assert larguras[-1] < 0.15, larguras
    assert ex.wilson(0, 0) is None


# ───────────────────────── §2 — peso SEMPRE relativo ───────────────────────────
@pytest.mark.unit
def test_o_peso_e_relativo_e_nunca_cita_valor_nem_percentual():
    """Regra 8 do método: o Erick fala em proporção à confirmação, nunca em cifra."""
    for estado, degrau in (("ativo", "meia posição"),
                           ("aguardar_rompimento", "inicial"),
                           ("aguardar_pullback", "inicial"),
                           ("sem_setup", "caixa")):
        p = ex.peso_relativo({"setup_state": estado})
        assert p["degrau"] == degrau, (estado, p)
        assert "%" not in p["degrau"] and "$" not in p["degrau"], p
        assert "relativo" in p["nota"], p


# ────────────────────────────── o card inteiro ────────────────────────────────
@pytest.mark.unit
def test_o_card_junta_tudo_e_e_serializavel():
    import json

    c = ex.card(_plano(), {"123": {"n": 9, "n_fechados": 3, "taxa_acerto": 0.66}})
    assert set(c) == {"veredito", "ordens", "invalidacao", "saida", "protecao",
                      "peso", "confiabilidade"}, set(c)
    assert c["invalidacao"]["price"] == 181.32, c["invalidacao"]
    json.dumps(c)   # vai pela HTTP: não pode ter nada não-serializável


@pytest.mark.unit
def test_o_limiar_do_card_e_o_MESMO_do_scanner():
    """Dois limiares com o mesmo nome e valores diferentes seria a lista dizendo "em
    gatilho" e o card dizendo "aguardar" sobre o mesmo preço."""
    assert ex._RR_RESIDUAL is _RR_RESIDUAL
    assert ex._GATILHO_TOL is _GATILHO_TOL


@pytest.mark.unit
def test_o_indice_declara_os_DOIS_setups_mesmo_com_ledger_vazio():
    """DENTE: o índice iterava só o que o ledger devolveu, então com track record
    vazio ou ilegível (o caminho fail-open do runner) o bloco SUMIA da tela — e um
    bloco ausente não diz "não há amostra", diz nada, justamente onde se decide."""
    c = ex.confiabilidade({})
    assert list(c["setups"]) == ["123", "storm"], c
    for nome, s in c["setups"].items():
        assert s["nivel"] == "insuficiente", (nome, s)
        assert s["taxa_acerto"] is None and s["n_fechados"] == 0, (nome, s)
        assert "amostra insuficiente (n=0)" in s["texto"], (nome, s)


@pytest.mark.unit
def test_a_ordem_dos_setups_nao_depende_do_que_o_ledger_mandou():
    """Um ledger que só tem Storm não pode fazer o Storm subir pro topo do card: a
    ordem da tela é a do ledger declarado, senão o bloco troca de lugar sozinho."""
    c = ex.confiabilidade({"storm": {"n": 40, "n_fechados": 25, "taxa_acerto": 0.6}})
    assert list(c["setups"]) == ["123", "storm"], c
    assert c["setups"]["storm"]["nivel"] == "operavel", c
    assert c["setups"]["123"]["n_fechados"] == 0, c
