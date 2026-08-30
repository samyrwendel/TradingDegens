"""A fronteira entre ESCREVER e PUBLICAR o front do TradingDegens.

O problema (medido, 29/08): ``_serve_static`` lia os arquivos do diretório do REPO a
cada requisição, e o cache-buster por mtime garantia que o navegador não segurasse a
versão anterior. Somando os dois, **o instante em que um agente salvava ``app.js``
era o instante em que o Samyr passava a ver aquilo** — sem commit, sem teste, sem
deploy. Ele viu o método Storm meio-escrito colado ao Padrão e reportou como defeito
de design; não era defeito, era obra em andamento aparecendo na tela dele.

O dano não é só o susto: passamos o dia tratando prints dele como evidência de
defeito, e parte do que ele reportou pode ter sido código a meio caminho. Isso
contamina o ciclo de revisão inteiro.

O QUE ESTE MÓDULO FAZ: o servidor deixa de ler o repo e passa a ler um diretório
PUBLICADO. Publicar é um passo explícito, e ele copia da **revisão COMMITADA**
(``git archive HEAD``), não do working tree.

**Por que da revisão commitada, e não uma cópia do working tree no deploy.** Um
snapshot do working tree moveria a fronteira, mas não a fecharia: o agente que salva
e reinicia o serviço no meio da edição publicaria o meio da edição do mesmo jeito. Do
HEAD, a regra fica sem exceção — *pra mudar a tela é preciso COMMITAR*. E isso alinha
o que o usuário vê com o que a suíte testou e o que o histórico registra, que é a
mesma coisa que o commit já significa em todo o resto do projeto.

**Atômico de propósito:** escreve num diretório temporário irmão e faz ``rename``. Uma
publicação nunca é vista pela metade — que é exatamente o defeito que ela veio matar.

**Modo AO VIVO** (``TRADINGDEGENS_STATIC_LIVE=1``): serve direto do repo, pra
desenvolver sem cerimônia. Desligado por padrão e barulhento no log quando ligado —
a conveniência do desenvolvedor não pode ser o padrão da tela do usuário.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# O diretório do REPO — a fonte de onde se publica, nunca o que se serve (salvo no
# modo ao vivo, explícito).
REPO_STATIC = Path(__file__).parent / "static"

# Caminho do repositório (raiz do git), derivado do próprio módulo.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Caminho, DENTRO do repo, que a publicação extrai.
_CAMINHO_NO_REPO = "tradingagents/webui/static"

_ENV_PUB = "TRADINGDEGENS_STATIC_PUB"
_ENV_LIVE = "TRADINGDEGENS_STATIC_LIVE"


def publicado_dir() -> Path:
    """Onde mora o front PUBLICADO (fora do repo, pra não ser tocado por edição)."""
    override = os.environ.get(_ENV_PUB)
    if override:
        return Path(override)
    return Path.home() / ".tradingagents" / "webui-publicado"


def modo_ao_vivo() -> bool:
    """Serve direto do repo? Só com a variável LIGADA explicitamente."""
    return os.environ.get(_ENV_LIVE, "").strip().lower() in ("1", "true", "sim", "yes")


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — argumentos fixos, sem shell
        ["git", *args], cwd=str(cwd or REPO_ROOT), capture_output=True, text=True,
        check=False, timeout=60,
    )


def revisao_publicada() -> str | None:
    """O commit que está publicado agora (gravado no ato da publicação)."""
    marca = publicado_dir() / ".revisao"
    try:
        return marca.read_text().strip() or None
    except OSError:
        return None


def publicar(revisao: str = "HEAD") -> dict[str, Any]:
    """Publica os estáticos da REVISÃO COMMITADA. Devolve o que aconteceu.

    Atômico: extrai num temporário irmão e troca por ``rename``. Falha em qualquer
    etapa deixa o publicado ANTERIOR intacto — publicação pela metade é o defeito que
    este módulo existe pra impedir, e ela não pode reaparecer pela porta do erro.
    """
    destino = publicado_dir()
    destino.parent.mkdir(parents=True, exist_ok=True)
    sha = _git("rev-parse", revisao)
    if sha.returncode != 0:
        raise RuntimeError(f"revisão {revisao!r} não resolve: {sha.stderr.strip()}")
    commit = sha.stdout.strip()

    tmp = Path(tempfile.mkdtemp(prefix=".publicando-", dir=str(destino.parent)))
    try:
        arch = subprocess.run(  # noqa: S603
            ["git", "archive", commit, _CAMINHO_NO_REPO],
            cwd=str(REPO_ROOT), capture_output=True, check=False, timeout=120,
        )
        if arch.returncode != 0:
            raise RuntimeError(f"git archive falhou: {arch.stderr.decode()[:200]}")
        with tarfile.open(fileobj=io.BytesIO(arch.stdout)) as tf:
            tf.extractall(tmp, filter="data")
        extraido = tmp / _CAMINHO_NO_REPO
        if not (extraido / "index.html").is_file():
            raise RuntimeError("a revisão não tem tradingagents/webui/static/index.html")
        (extraido / ".revisao").write_text(commit + "\n")
        antigo = destino.with_name(destino.name + ".antigo")
        shutil.rmtree(antigo, ignore_errors=True)
        if destino.exists():
            destino.rename(antigo)
        extraido.rename(destino)
        shutil.rmtree(antigo, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    arquivos = sorted(p.name for p in destino.iterdir() if p.is_file())
    logger.info("front publicado: %s -> %s (%d arquivos)", commit[:12], destino, len(arquivos))
    return {"revisao": commit, "destino": str(destino), "arquivos": arquivos}


def versao_do_asset(diretorio: Path, arquivo: str) -> str:
    """O ``?v=`` do cache-buster, para ``arquivo`` servido de ``diretorio``.

    Publicado → a REVISÃO (sha curto). O mtime não serve aqui: ``git archive``
    carimba os arquivos com a data do COMMIT, então duas publicações no mesmo
    segundo saem com o mesmo mtime — e publicar um commit ANTERIOR produziria um
    mtime menor que o já cacheado, deixando o navegador com a versão velha. O sha
    muda exatamente quando o conteúdo publicado muda, que é o que o buster promete.

    Modo ao vivo / fallback sem git → o mtime de sempre, que é o certo ali: o
    arquivo muda a cada gravação e é isso que se quer ver.
    """
    marca = diretorio / ".revisao"
    try:
        sha = marca.read_text().strip()
        if sha:
            return sha[:12]
    except OSError:
        pass
    f = diretorio / arquivo
    try:
        return str(int(f.stat().st_mtime))
    except OSError:
        return "0"


def static_dir(*, bootstrap: bool = True) -> Path:
    """De onde o servidor LÊ os estáticos.

    1. modo ao vivo ligado → o repo, com aviso no log (é a exceção declarada);
    2. publicado existente → o publicado (o caminho normal);
    3. nada publicado ainda → tenta publicar do HEAD agora (primeiro boot); se não
       der (sem git, repo ausente), cai no repo e DIZ isso no log — servir nada seria
       pior, e o aviso é o que impede o fallback de virar o comportamento silencioso
       de sempre.
    """
    if modo_ao_vivo():
        logger.warning("MODO AO VIVO: servindo o front direto do repo (%s) — "
                       "edição aparece na tela sem publicar", REPO_STATIC)
        return REPO_STATIC
    pub = publicado_dir()
    if (pub / "index.html").is_file():
        return pub
    if bootstrap:
        try:
            publicar()
            if (pub / "index.html").is_file():
                return pub
        except Exception as exc:  # noqa: BLE001 — nunca deixa o servidor sem front
            logger.warning("não deu pra publicar o front no boot (%s) — servindo do "
                           "repo; PUBLIQUE antes de considerar a tela confiável", exc)
    return REPO_STATIC


def main(argv: list[str] | None = None) -> int:
    """CLI do deploy do front: ``python -m tradingagents.webui.static_publish``."""
    import argparse

    ap = argparse.ArgumentParser(description="Publica o front do TradingDegens.")
    ap.add_argument("revisao", nargs="?", default="HEAD",
                    help="revisão git a publicar (padrão: HEAD)")
    ap.add_argument("--onde", action="store_true", help="só mostra o que está publicado")
    a = ap.parse_args(argv)
    if a.onde:
        print(f"publicado em: {publicado_dir()}")
        print(f"revisão:      {revisao_publicada() or '(nada publicado)'}")
        print(f"modo ao vivo: {'LIGADO' if modo_ao_vivo() else 'desligado'}")
        return 0
    out = publicar(a.revisao)
    print(f"publicado {out['revisao'][:12]} em {out['destino']}")
    print("arquivos: " + ", ".join(out["arquivos"]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
