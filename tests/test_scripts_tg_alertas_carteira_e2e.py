"""Ponta a ponta do alerta HORÁRIO da carteira do Erick (task 20260902-053).

Corrige a cadência da task 20260902-051 (diária → 1h, ordem direta do Samyr) e
prova, sobre o snapshot REAL salvo em ``~/brain/trading-ops/erick-carteira/`` com
uma mudança SINTÉTICA por cima: o alerta chega com os campos pedidos, silêncio
quando nada muda (mesmo em 24 leituras seguidas), oscilação de preço sem
mudança de QUANTIDADE não dispara nada, chat de grupo é recusado, e falha de
acesso à fonte só vira alerta depois de >24h — uma vez por episódio, não a cada
hora enquanto durar.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from tradingagents.dataflows import erick_carteira as ec

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "tg_alertas.py"
_SNAPSHOT = (Path.home() / "brain" / "trading-ops" / "erick-carteira"
             / "carteira-2026-09-01.json")


def _carrega_script():
    spec = importlib.util.spec_from_file_location("tg_alertas_e2e", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _base() -> dict:
    return json.loads(_SNAPSHOT.read_text(encoding="utf-8"))


def _leitura(carteira: dict, *, degradado: bool = False, idade: float = 0.1,
            lido_em: float = 0.0) -> dict:
    return {"carteira": carteira, "historico": None, "lido_em": lido_em,
            "idade_horas": idade, "degradado": degradado, "fonte": "https://exemplo"}


@pytest.fixture
def tg(tmp_path, monkeypatch):
    mod = _carrega_script()
    monkeypatch.setattr(mod, "_ULTIMA_CARTEIRA", tmp_path / "ultima.json")
    monkeypatch.setattr(mod, "_FALHA_ALERTADA", tmp_path / "falha.json")
    enviados: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_envia",
                        lambda chat, texto, topico="": (enviados.append((chat, texto)) or True))
    return mod, enviados


# ── (a) mudança sintética sobre o snapshot real ────────────────────────────────
def test_a_mudanca_sintetica_gera_UM_alerta_na_DM_com_todos_os_campos(tg, monkeypatch):
    mod, enviados = tg
    base = _base()
    depois_dict = copy.deepcopy(base)
    for a in depois_dict["ativos"]:
        if a["ticker"] == "ASTS":
            a["qtd"] = 200.0          # comprou mais
        if a["ticker"] == "CASH":
            a["qtd"] -= 9000.0        # caixa caiu na mesma proporção

    monkeypatch.setenv("ALERTA_CARTEIRA_CHAT", "30289486")
    monkeypatch.setattr(ec, "carteira", lambda **kw: _leitura(base))
    assert mod.carteira() == 0
    assert enviados == [], "a PRIMEIRA leitura só grava a baseline, não anuncia nada"

    monkeypatch.setattr(ec, "carteira", lambda **kw: _leitura(depois_dict))
    assert mod.carteira() == 0
    assert len(enviados) == 1
    chat, texto = enviados[0]
    assert chat == "30289486"
    assert "*" not in texto                                   # DA-034: zero markdown
    assert "ASTS" in texto and "AUMENTOU" in texto              # o ativo e a ação
    assert "147" in texto and "200" in texto                    # qtd antes → depois
    assert "% do capital" in texto                              # % do capital
    assert "caixa" in texto.lower() and "→" in texto            # caixa antes → depois
    assert "pivô de alta" in texto                              # racional dele (feed ASTS)
    for linha in texto.split("\n"):
        assert len(linha) <= 60, linha


# ── (b) silêncio absoluto sem mudança, mesmo em 24 leituras ────────────────────
def test_24_leituras_sem_mudanca_ZERO_mensagens(tg, monkeypatch):
    mod, enviados = tg
    base = _base()
    monkeypatch.setenv("ALERTA_CARTEIRA_CHAT", "30289486")
    monkeypatch.setattr(ec, "carteira", lambda **kw: _leitura(base))
    for _ in range(24):
        assert mod.carteira() == 0
    assert enviados == []


# ── (c) oscilação de preço sem mudança de quantidade não dispara nada ──────────
def test_oscilacao_de_preco_sem_mudanca_de_qtd_ZERO_mensagens(tg, monkeypatch):
    mod, enviados = tg
    base = _base()
    depois_dict = copy.deepcopy(base)
    for a in depois_dict["ativos"]:
        if a["ticker"] == "MSFT":
            a["precoMedio"] = a["precoMedio"] * 1.3   # preço mudou, qtd não

    monkeypatch.setenv("ALERTA_CARTEIRA_CHAT", "30289486")
    monkeypatch.setattr(ec, "carteira", lambda **kw: _leitura(base))
    mod.carteira()
    monkeypatch.setattr(ec, "carteira", lambda **kw: _leitura(depois_dict))
    assert mod.carteira() == 0
    assert enviados == []


# ── (d) chat de grupo é recusado ────────────────────────────────────────────────
def test_chat_de_grupo_e_RECUSADO(tg, monkeypatch):
    mod, enviados = tg
    base = _base()
    monkeypatch.setenv("ALERTA_CARTEIRA_CHAT", "-1001234567890")
    monkeypatch.setattr(ec, "carteira", lambda **kw: _leitura(base))
    assert mod.carteira() == 2
    assert enviados == []


# ── falha de acesso: silêncio até passar de 24h, depois avisa 1 vez só ─────────
def test_falha_de_acesso_ATE_24h_nao_alerta(tg, monkeypatch):
    mod, enviados = tg
    base = _base()
    monkeypatch.setenv("ALERTA_CARTEIRA_CHAT", "30289486")
    monkeypatch.setattr(ec, "carteira",
                        lambda **kw: _leitura(base, degradado=True, idade=23.9))
    assert mod.carteira() == 0
    assert enviados == []


def test_falha_de_acesso_ACIMA_de_24h_avisa_UMA_VEZ_por_episodio(tg, monkeypatch):
    mod, enviados = tg
    base = _base()
    monkeypatch.setenv("ALERTA_CARTEIRA_CHAT", "30289486")
    monkeypatch.setattr(ec, "carteira",
                        lambda **kw: _leitura(base, degradado=True, idade=30.0, lido_em=111.0))
    assert mod.carteira() == 0
    assert len(enviados) == 1 and "30h" in enviados[0][1]
    # próxima leitura, MESMO episódio (mesmo lido_em) — não repete o aviso
    assert mod.carteira() == 0
    assert len(enviados) == 1


def test_falha_recupera_e_um_episodio_NOVO_de_falha_avisa_de_novo(tg, monkeypatch):
    mod, enviados = tg
    base = _base()
    monkeypatch.setenv("ALERTA_CARTEIRA_CHAT", "30289486")
    monkeypatch.setattr(ec, "carteira",
                        lambda **kw: _leitura(base, degradado=True, idade=30.0, lido_em=111.0))
    mod.carteira()
    assert len(enviados) == 1
    # volta a ler ao vivo — reseta o marcador de episódio de falha
    monkeypatch.setattr(ec, "carteira", lambda **kw: _leitura(base))
    mod.carteira()
    # um NOVO episódio de falha, com outro lido_em, avisa de novo
    monkeypatch.setattr(ec, "carteira",
                        lambda **kw: _leitura(base, degradado=True, idade=25.0, lido_em=222.0))
    mod.carteira()
    assert len(enviados) == 2
