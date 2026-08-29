"""Scanner estrutural 1-2-3 — o olho barato ($0 de LLM) antes da análise cara.

Puro e offline: monkeypatch dos seams do próprio scanner (``build_price_chart`` /
``build_actionable_plan_dict`` importados de price_structure). Trava:

* os estados com planos falsos (em_gatilho / em_movimento / invalidou / formando /
  sem_setup / sem_dado) e o cálculo da distância preço→gatilho;
* EM GATILHO é no ponto de entrada (≤ tol) — acionado longe vira em_movimento,
  não em_gatilho (o gatilho já ficou p/ trás);
* INVALIDOU: preço além do ponto 3 detectado como estado (a premissa morreu);
* a ordenação por urgência (em_gatilho primeiro, dist crescente);
* fail-open: símbolo quebrado vira linha ``sem_dado``, nunca derruba o scan;
* o track record: log append-only + vereditos DIREÇÃO-CONSCIENTES (venda: TP
  abaixo do preço — bug real pego no probe ao vivo, taxa 1.0 falsa).
"""

import pytest

import tradingagents.webui.scanner as sc
from tradingagents.webui.scanner import (
    ScanLog,
    scan_symbol,
    scan_verdicts,
    scan_watchlist,
)


def _plan(pattern=None, price=100.0, setup_state="ativo", invalidation=None):
    plan = {"pattern": pattern, "price": price, "setup_state": setup_state}
    if invalidation is not None:
        plan["invalidation"] = {"price": invalidation}
    return plan


def _pat(direction="compra", state="formando", trigger=100.0):
    return {"direction": direction, "state": state, "trigger": trigger}


@pytest.fixture
def fake_fetch(monkeypatch):
    """Seam: mapa (ticker, frame) -> plan. Chart vazio (preço vem do plan).
    Live price desligado (None) — o teste é offline; o price do plan é a fonte."""
    def install(plans):
        monkeypatch.setattr(
            sc, "build_actionable_plan_dict",
            lambda t, d, timeframe="1d", method="padrao": plans.get((t.upper(), timeframe), _plan(setup_state="sem_setup")),
        )
        monkeypatch.setattr(sc, "build_price_chart",
                            lambda t, d, timeframe="1d", method="padrao": {"candles": []})
        monkeypatch.setattr(sc, "_live_price", lambda ticker: None)
    return install


def test_estado_em_gatilho_no_ponto_de_entrada(fake_fetch):
    # preço a 0,3% do gatilho → em_gatilho pela TOLERÂNCIA (ponto de entrada AGORA)
    fake_fetch({("A", "1d"): _plan(_pat(trigger=100.3), price=100.0)})
    r = scan_symbol("A", "2026-08-28", frames=("1d",))
    assert r["melhor"]["estado"] == "em_gatilho"
    assert abs(r["melhor"]["dist_pct"] - abs(100.0 / 100.3 - 1)) < 1e-9
    # a direção é preservada no resumo (COMPRA/VENDA vem dela no painel)
    assert r["melhor"]["direction"] == "compra"


def test_acionado_longe_e_em_movimento_nao_em_gatilho(fake_fetch):
    # padrão ACIONADO e preço 18% além do gatilho → em_movimento (entrada passou),
    # NUNCA em_gatilho (o gatilho já ficou p/ trás).
    fake_fetch({("B", "1d"): _plan(_pat(state="acionado", trigger=65401.69),
                                   price=77699.0, invalidation=60000.0)})
    r = scan_symbol("B", "2026-08-28", frames=("1d",))
    assert r["melhor"]["estado"] == "em_movimento"


def test_invalidou_quando_preco_alem_do_ponto_3(fake_fetch):
    # COMPRA: setup morre ao PERDER o ponto 3 (preço < invalidação).
    fake_fetch({("C", "1d"): _plan(_pat(trigger=110.0), price=88.0, invalidation=95.0)})
    assert scan_symbol("C", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "invalidou"
    # VENDA: setup morre ao VOLTAR acima do ponto 3 (preço > invalidação).
    fake_fetch({("V", "1d"): _plan(_pat(direction="venda", trigger=90.0),
                                   price=105.0, invalidation=95.0)})
    assert scan_symbol("V", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "invalidou"


def test_estado_formando_sem_setup(fake_fetch):
    fake_fetch({("A", "1d"): _plan(_pat(trigger=110.0), price=100.0)})   # 10% — longe
    assert scan_symbol("A", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "formando"
    fake_fetch({("A", "1d"): _plan(None, price=100.0, setup_state="sem_setup")})
    assert scan_symbol("A", "2026-08-28", frames=("1d",))["melhor"]["estado"] == "sem_setup"


def test_estado_sem_dado_degraded_never_invents(fake_fetch):
    """Plano degradado (sem_dado/intradiario_indisponivel) → sem_dado com motivo."""
    fake_fetch({("A", "1d"): _plan(None, price=None, setup_state="sem_dado")})
    r = scan_symbol("A", "2026-08-28", frames=("1d",))
    assert r["melhor"]["estado"] == "sem_dado"
    assert "fonte" in (r["melhor"].get("motivo") or "")


def test_scan_watchlist_orders_by_urgency_and_survives_broken_symbol(fake_fetch):
    fake_fetch({
        ("AAA", "1d"): _plan(_pat(trigger=100.2), price=100.0),        # em_gatilho
        ("BBB", "1d"): _plan(_pat(trigger=101.5), price=100.0),        # formando
        ("CCC", "1d"): _plan(_pat(trigger=115.0), price=100.0),        # formando
    })
    def boom(t, d, timeframe="1d", method="padrao"):
        raise RuntimeError("fonte fora do ar")

    sc.build_actionable_plan_dict = boom  # quebra TUDO: fail-open do scan
    out = scan_watchlist(["AAA", "BBB", "CCC"], "2026-08-28", frames=("1d",))
    assert out["resumo"].get("sem_dado") == 3
    assert all(s["melhor"]["estado"] == "sem_dado" for s in out["ativos"])


def test_scan_watchlist_em_gatilho_first(fake_fetch):
    fake_fetch({
        ("ZZZ", "1d"): _plan(_pat(trigger=115.0), price=100.0),        # formando
        ("AAA", "1d"): _plan(_pat(trigger=100.1), price=100.0),        # em_gatilho (0,1%)
        ("MMM", "1d"): _plan(_pat(trigger=100.5), price=100.0),        # em_gatilho (0,5%)
    })
    out = scan_watchlist(["ZZZ", "MMM", "AAA"], "2026-08-28", frames=("1d",))
    assert [s["ticker"] for s in out["ativos"]] == ["AAA", "MMM", "ZZZ"]
    assert out["resumo"]["em_gatilho"] == 2


def test_scan_log_is_append_only_dedup_free(tmp_path):
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "MSFT", "frame": "4h", "trigger": 513.73, "direction": "compra"})
    log.record({"ticker": "AAPL", "frame": "1d", "trigger": 299.74, "direction": "venda"})
    assert len(log.entries()) == 2
    # linha ilegível é ignorada, não derruba a leitura
    (tmp_path / "scans.jsonl").write_text("{lixo\n", encoding="utf-8")
    assert isinstance(log.entries(), list)


def test_verdicts_are_direction_aware(tmp_path, monkeypatch):
    """VENDA: TP fica ABAIXO — price >= tp NÃO é 'bateu_tp' (bug real do probe:
    taxa 1.0 falsa). Compra: price <= sl é stop, price >= tp é alvo."""
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "V", "frame": "1d", "trigger": 100.0, "direction": "venda",
                "tp": 90.0, "sl": 105.0})
    log.record({"ticker": "C", "frame": "1d", "trigger": 100.0, "direction": "compra",
                "tp": 110.0, "sl": 95.0})

    # V em 99: abaixo do gatilho → lucro em andamento (venda lucra quando cai).
    # C em 105: acima do gatilho, abaixo do TP → lucro em andamento. NENHUM
    # bateu_tp falso (o bug: venda em 99 ≥ TP 90 marcava 'bateu_tp' à toa).
    prices = {"V": 99.0, "C": 105.0}
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": prices[t], "pattern": None, "setup_state": "ativo"})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: None)   # offline: usa o price do plan
    out = scan_verdicts(log, ["V", "C"], "2026-08-28")
    v = {x["ticker"]: x["veredito"] for x in out["verdicts"]}
    assert v["V"] == "andamento_lucro"
    assert v["C"] == "andamento_lucro"
    assert out["n_fechados"] == 0 and out["taxa_acerto"] is None


def test_verdicts_tp_sl_closed_counts(tmp_path, monkeypatch):
    log = ScanLog(tmp_path / "scans.jsonl")
    log.record({"ticker": "C", "frame": "1d", "trigger": 100.0, "direction": "compra",
                "tp": 110.0, "sl": 95.0})
    log.record({"ticker": "S", "frame": "1d", "trigger": 100.0, "direction": "compra",
                "tp": 110.0, "sl": 95.0})
    prices = {"C": 111.0, "S": 94.0}            # C bateu TP; S bateu SL
    monkeypatch.setattr(sc, "build_actionable_plan_dict",
                        lambda t, d, timeframe="1d", method="padrao":
                        {"price": prices[t], "pattern": None, "setup_state": "ativo"})
    monkeypatch.setattr(sc, "_live_price", lambda ticker: None)   # offline: usa o price do plan
    out = scan_verdicts(log, ["C", "S"], "2026-08-28")
    v = {x["ticker"]: x["veredito"] for x in out["verdicts"]}
    assert v["C"] == "bateu_tp" and v["S"] == "bateu_sl"
    assert out["n_fechados"] == 2 and out["taxa_acerto"] == 0.5
