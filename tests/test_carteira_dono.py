"""carteira_dono_tickers — os tickers da carteira REAL do dono (task 20260903-021).

O Storm acertou o MSFT 4h e o gatilho não tinha onde entrar porque o ticker não
estava coberto pela watchlist da agenda. O arquivo vive fora do ``watchlist.json``
curado pela tela (:class:`WatchlistStore`) — é editado à mão pelo dono, sem
endpoint HTTP de escrita — e a leitura é sempre fail-open: nunca deve derrubar a
passada agendada por causa de um arquivo ausente ou corrompido.
"""
import json

import pytest

from tradingagents.webui.store import carteira_dono_tickers

pytestmark = pytest.mark.unit


def test_arquivo_ausente_devolve_lista_vazia(tmp_path):
    assert carteira_dono_tickers(tmp_path / "nao-existe.json") == []


def test_le_a_lista_gravada(tmp_path):
    p = tmp_path / "carteira-dono.json"
    p.write_text(json.dumps({"tickers": [{"ticker": "MSFT"}, {"ticker": "META"}]}),
                encoding="utf-8")
    assert carteira_dono_tickers(p) == [{"ticker": "MSFT"}, {"ticker": "META"}]


def test_json_corrompido_e_fail_open(tmp_path):
    p = tmp_path / "carteira-dono.json"
    p.write_text("{ nao é json", encoding="utf-8")
    assert carteira_dono_tickers(p) == []


def test_arquivo_sem_a_chave_tickers_e_fail_open(tmp_path):
    p = tmp_path / "carteira-dono.json"
    p.write_text(json.dumps({"outra_coisa": 1}), encoding="utf-8")
    assert carteira_dono_tickers(p) == []


def test_env_var_isola_o_caminho_default(tmp_path, monkeypatch):
    """Mesmo molde de override do ``clone_erick._base_dir`` (``CLONE_ERICK_DIR``):
    a suíte usa ``CARTEIRA_DONO_PATH`` pra nunca tocar o arquivo real desta
    máquina (ver a fixture autouse ``_isola_carteira_dono`` em conftest.py)."""
    p = tmp_path / "via-env.json"
    p.write_text(json.dumps({"tickers": [{"ticker": "DELL"}]}), encoding="utf-8")
    monkeypatch.setenv("CARTEIRA_DONO_PATH", str(p))
    assert carteira_dono_tickers() == [{"ticker": "DELL"}]


def test_argumento_explicito_vence_o_env(tmp_path, monkeypatch):
    via_env = tmp_path / "via-env.json"
    via_env.write_text(json.dumps({"tickers": [{"ticker": "DELL"}]}), encoding="utf-8")
    monkeypatch.setenv("CARTEIRA_DONO_PATH", str(via_env))
    explicito = tmp_path / "explicito.json"
    explicito.write_text(json.dumps({"tickers": [{"ticker": "MSTR"}]}), encoding="utf-8")
    assert carteira_dono_tickers(explicito) == [{"ticker": "MSTR"}]
