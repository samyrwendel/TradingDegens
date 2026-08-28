"""Finnhub como fonte de CONFERÊNCIA de fundamentals — FCF TTM, shares, quote.

Trava as três garantias do módulo (offline, monkeypatch no único seam de rede):

* a DERIVAÇÃO do FCF TTM (quote ÷ pfcfShareTTM × shares) com números fixos;
* fail-open: key ausente ou endpoint quebrado → ``None``, nunca exceção, nunca
  número inventado;
* shares vêm em MILHÕES do profile2 e saem em unidades absolutas.
"""
import pytest

import tradingagents.dataflows.finnhub_earnings as fe
import tradingagents.dataflows.finnhub_fundamentals as ff


def _fake_api(monkeypatch, payload: dict):
    """Um _finnhub_get falso por path: {'quote': ..., 'stock/metric': ..., ...}."""
    monkeypatch.setattr(fe, "get_api_key", lambda: "k-teste")
    monkeypatch.setattr(ff, "get_api_key", lambda: "k-teste")
    monkeypatch.setattr(ff, "_finnhub_get", lambda path, params: payload[path])


def test_fcf_ttm_derivation_with_fixed_numbers(monkeypatch):
    """price 100 · pfcfShareTTM 10 → FCF/ação 10; shares 2000M → FCF total 20 bi."""
    _fake_api(monkeypatch, {
        "stock/metric": {"metric": {"pfcfShareTTM": 10.0}},
        "quote": {"c": 100.0},
        "stock/profile2": {"shareOutstanding": 2000},
    })
    assert ff.get_fcf_ttm("INTC") == pytest.approx(20_000_000_000.0)


def test_fcf_ttm_missing_metric_is_none(monkeypatch):
    _fake_api(monkeypatch, {
        "stock/metric": {"metric": {}},
        "quote": {"c": 100.0},
        "stock/profile2": {"shareOutstanding": 2000},
    })
    assert ff.get_fcf_ttm("INTC") is None


def test_fcf_ttm_without_key_is_none(monkeypatch):
    monkeypatch.setattr(fe, "get_api_key", lambda: None)
    monkeypatch.setattr(ff, "get_api_key", lambda: None)
    assert ff.get_fcf_ttm("INTC") is None


def test_all_endpoints_fail_open(monkeypatch):
    """Endpoint estourando HTTP → None em TODAS as funções (nunca exceção)."""
    def boom(path, params):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(fe, "get_api_key", lambda: "k-teste")
    monkeypatch.setattr(ff, "get_api_key", lambda: "k-teste")
    monkeypatch.setattr(ff, "_finnhub_get", boom)
    assert ff.get_quote("INTC") is None
    assert ff.get_shares("INTC") is None
    assert ff.get_fcf_ttm("INTC") is None


def test_shares_converted_from_millions(monkeypatch):
    _fake_api(monkeypatch, {"stock/profile2": {"shareOutstanding": 5044}})
    assert ff.get_shares("INTC") == pytest.approx(5_044_000_000.0)


def test_negative_pfcf_is_none(monkeypatch):
    """P/FCF negativo (FCF negativo) não gera FCF 'negativo' inventado — None."""
    _fake_api(monkeypatch, {
        "stock/metric": {"metric": {"pfcfShareTTM": -10.0}},
        "quote": {"c": 100.0},
        "stock/profile2": {"shareOutstanding": 2000},
    })
    assert ff.get_fcf_ttm("INTC") is None
