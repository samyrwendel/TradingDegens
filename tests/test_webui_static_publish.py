"""A fronteira entre ESCREVER e PUBLICAR o front (task 20260830-001).

O defeito, medido no serviço no ar: ``_serve_static`` lia o diretório do REPO a cada
requisição e o cache-buster por mtime impedia o navegador de segurar a versão
anterior. Somados, **o instante em que um agente salvava ``app.js`` era o instante em
que o usuário via aquilo** — sem commit, sem teste, sem deploy. O Samyr viu o método
Storm meio-escrito colado ao Padrão e reportou como defeito de design; era obra em
andamento na tela dele.

O dano maior nem é o susto: o dia inteiro foi passado tratando prints dele como
evidência de defeito, e parte do que ele reportou pode ter sido código a meio
caminho. Isso contamina o ciclo de revisão inteiro.

O que estes testes travam:
  (a) working tree editado e NÃO publicado → a resposta HTTP é a PUBLICADA;
  (b) depois de publicar → é a nova, e o cache-buster acompanha (o usuário não
      precisa limpar nada — aquilo resolveu um problema real e não regride);
  (c) publicar copia da REVISÃO COMMITADA, não do working tree — a fronteira sem
      exceção: pra mudar a tela é preciso COMMITAR;
  (d) o modo AO VIVO existe pra desenvolver, é explícito e está DESLIGADO por padrão;
  (e) publicação é atômica: falha no meio deixa o publicado anterior intacto.
"""

import os
import re
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from tradingagents.webui import static_publish as sp
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.store import HistoryStore


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Um repositório git de mentira com o mesmo layout do de verdade."""
    raiz = tmp_path / "repo"
    estatico = raiz / "tradingagents" / "webui" / "static"
    estatico.mkdir(parents=True)
    (estatico / "index.html").write_text(
        '<html><head><link href="/static/style.css"></head>'
        '<body><script src="/static/app.js"></script></body></html>')
    (estatico / "app.js").write_text("// PUBLICADO\n")
    (estatico / "style.css").write_text("/* PUBLICADO */\n")
    _git(raiz, "init", "-q")
    _git(raiz, "add", "-A")
    _git(raiz, "commit", "-qm", "publicado")
    monkeypatch.setattr(sp, "REPO_ROOT", raiz)
    monkeypatch.setattr(sp, "REPO_STATIC", estatico)
    monkeypatch.setenv("TRADINGDEGENS_STATIC_PUB", str(tmp_path / "publicado"))
    monkeypatch.delenv("TRADINGDEGENS_STATIC_LIVE", raising=False)
    return raiz


def _estatico(repo_dir: Path) -> Path:
    return repo_dir / "tradingagents" / "webui" / "static"


# ---------------------------------------------------------------- o mecanismo --
def test_publicar_copia_da_revisao_COMMITADA_e_nao_do_working_tree(repo):
    """O coração da correção. Um snapshot do working tree moveria a fronteira sem
    fechá-la: o agente que salva e reinicia no meio da edição publicaria o meio da
    edição do mesmo jeito. Do HEAD, a regra não tem exceção."""
    sp.publicar()
    pub = sp.publicado_dir()
    assert (pub / "app.js").read_text() == "// PUBLICADO\n"

    # agora um agente EDITA e NÃO commita
    (_estatico(repo) / "app.js").write_text("// EM EDIÇÃO — METADE DE UMA FEATURE\n")
    sp.publicar()
    assert (pub / "app.js").read_text() == "// PUBLICADO\n", (
        "publicar pegou o working tree — a fronteira não existe")

    # e commitar é o que muda a tela
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature pronta")
    sp.publicar()
    assert "EM EDIÇÃO" in (pub / "app.js").read_text()


def test_a_revisao_publicada_fica_registrada(repo):
    out = sp.publicar()
    assert sp.revisao_publicada() == out["revisao"]
    assert len(out["revisao"]) == 40


def test_publicacao_e_ATOMICA_e_falha_nao_derruba_o_publicado(repo, monkeypatch):
    """Publicação pela metade é o defeito que este módulo veio matar — ela não pode
    voltar pela porta do erro."""
    sp.publicar()
    pub = sp.publicado_dir()
    antes = (pub / "app.js").read_text()
    with pytest.raises(RuntimeError):
        sp.publicar("revisao-que-nao-existe")
    assert (pub / "app.js").read_text() == antes
    assert (pub / "index.html").is_file()


def test_modo_AO_VIVO_e_explicito_e_desligado_por_padrao(repo, monkeypatch):
    """Conveniência de quem desenvolve não pode ser o padrão da tela do usuário."""
    assert sp.modo_ao_vivo() is False
    assert sp.static_dir() == sp.publicado_dir()
    monkeypatch.setenv("TRADINGDEGENS_STATIC_LIVE", "1")
    assert sp.modo_ao_vivo() is True
    assert sp.static_dir() == sp.REPO_STATIC


def test_primeiro_boot_publica_sozinho_em_vez_de_servir_o_repo(repo):
    """Sem nada publicado, o servidor não cai calado no repo: ele PUBLICA do HEAD."""
    assert not (sp.publicado_dir() / "index.html").exists()
    assert sp.static_dir() == sp.publicado_dir()
    assert (sp.publicado_dir() / "index.html").is_file()


def test_sem_git_o_fallback_e_declarado_e_nao_silencioso(repo, monkeypatch, caplog):
    """Servir NADA seria pior; o que não pode é o fallback virar o comportamento
    silencioso de sempre."""
    monkeypatch.setattr(sp, "REPO_ROOT", Path("/nao/existe"))
    with caplog.at_level("WARNING"):
        assert sp.static_dir() == sp.REPO_STATIC
    assert any("PUBLIQUE" in r.message or "publicar" in r.message for r in caplog.records), caplog.text


# ------------------------------------------------------------- pela porta HTTP --
@pytest.fixture
def servidor(repo, tmp_path, monkeypatch):
    """O servidor de verdade, apontado pro publicado do repo de mentira."""
    from tradingagents.webui import server as sv

    sp.publicar()
    monkeypatch.setattr(sv, "_STATIC_DIR", sp.publicado_dir())
    runner = AnalysisRunner(
        base_config={"results_dir": str(tmp_path / "res"), "llm_provider": "openai",
                     "deep_think_llm": "x", "quick_think_llm": "y"},
        store=HistoryStore(tmp_path / "res"))
    httpd = sv.make_server("127.0.0.1", 0, runner=runner)
    porta = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{porta}"
    finally:
        httpd.shutdown()


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read().decode()


def test_editar_sem_publicar_NAO_muda_o_que_o_servidor_responde(repo, servidor):
    """(a) do critério: enquanto uma task edita o front, a tela continua mostrando a
    ÚLTIMA versão publicada, inteira e coerente."""
    assert "PUBLICADO" in _get(servidor + "/static/app.js")
    (_estatico(repo) / "app.js").write_text("// METADE DE UMA FEATURE NOVA\n")
    (_estatico(repo) / "style.css").write_text("/* METADE */\n")
    assert "PUBLICADO" in _get(servidor + "/static/app.js"), (
        "o working tree vazou pra resposta HTTP")
    assert "METADE" not in _get(servidor + "/static/style.css")


def test_depois_de_publicar_a_tela_muda_e_o_cache_buster_acompanha(repo, servidor):
    """(b) do critério: o deploy continua explícito, e depois dele o usuário NÃO
    precisa limpar cache — o ?v= por mtime resolveu um problema real e fica."""
    v_antes = re.search(r"/static/app\.js\?v=([0-9a-f]+)", _get(servidor + "/")).group(1)
    (_estatico(repo) / "app.js").write_text("// VERSÃO NOVA\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "nova")
    assert "PUBLICADO" in _get(servidor + "/static/app.js"), "ainda não publiquei"

    sp.publicar()   # o passo EXPLÍCITO de deploy

    assert "VERSÃO NOVA" in _get(servidor + "/static/app.js")
    v_depois = re.search(r"/static/app\.js\?v=([0-9a-f]+)", _get(servidor + "/")).group(1)
    # DENTE: enquanto o buster era o mtime, duas publicações no mesmo segundo saíam
    # com o MESMO ?v= — `git archive` carimba os arquivos com a data do COMMIT, não
    # a de agora. O buster passou a ser a REVISÃO, que muda quando o conteúdo muda.
    assert v_depois != v_antes, ("o cache-buster tem que mudar com a publicação, "
                                 "senão o navegador segura a versão antiga")


def test_o_index_publicado_referencia_os_assets_publicados(servidor):
    """Coerência do conjunto: o index e os assets vêm da MESMA publicação — era a
    incoerência entre eles (HTML velho + JS novo) que punha o Storm meio-desenhado
    na tela."""
    html = _get(servidor + "/")
    assert re.search(r"/static/app\.js\?v=[0-9a-f]+", html), html
    assert re.search(r"/static/style\.css\?v=[0-9a-f]+", html), html
