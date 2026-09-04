"""Porta 6 (DA-189) no TradingDegens: rate limit no login (força-bruta), teto de
rajada nas rotas caras, e teto de gasto com recusa explícita.

Prova com dente: sobe o servidor num socket real e martela as rotas de verdade
(não só chama o limiter no vazio)."""

import json
import os
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

from tests.test_webui_runner import _factory
from tradingagents.webui import ratelimit, spend_guard
from tradingagents.webui.auth import OwnerAuth
from tradingagents.webui.runner import AnalysisRunner
from tradingagents.webui.server import make_server
from tradingagents.webui.store import HistoryStore


# ── helpers de servidor ──────────────────────────────────────────────────────
def _serve(tmp_path, *, login_limiter=None, expensive_limiter=None, auth=None):
    runner = AnalysisRunner(base_config={"results_dir": str(tmp_path)},
                            store=HistoryStore(tmp_path), graph_factory=_factory())
    httpd = make_server("127.0.0.1", 0, runner=runner, auth=auth,
                        login_limiter=login_limiter,
                        expensive_limiter=expensive_limiter)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def _post(base, path, payload, *, opener=None, headers=None):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers=hdrs)
    _open = opener.open if opener is not None else urllib.request.urlopen
    try:
        with _open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, (json.loads(e.read() or b"{}")), dict(e.headers)


# ── login: força-bruta → 429 com lockout (critério 1) ────────────────────────
def test_login_brute_force_vira_429_com_lockout(tmp_path):
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = "senha-secreta"
    try:
        limiter = ratelimit.RateLimiter(max_hits=3, window_s=300, block_base_s=60)
        httpd, base = _serve(tmp_path, login_limiter=limiter, auth=OwnerAuth())
        try:
            codes = []
            for _ in range(3):
                codes.append(_post(base, "/api/login", {"password": "errada"})[0])
            # As 3 primeiras tentativas passam pelo limiter e batem em 401 (senha ruim).
            assert codes == [401, 401, 401]
            # A 4ª estoura o teto: 429 com Retry-After, SEM revelar nada da senha.
            status, body, headers = _post(base, "/api/login", {"password": "errada"})
            assert status == 429
            assert body["error_code"] == "rate_limited"
            assert int(headers.get("Retry-After", "0")) >= 1
            # Até com a senha CERTA o IP bloqueado leva 429 (o gate é antes do verify) —
            # e o corpo é idêntico, não vaza que a senha estava certa.
            status2, body2, _ = _post(base, "/api/login", {"password": "senha-secreta"})
            assert status2 == 429 and body2["error_code"] == "rate_limited"
        finally:
            httpd.shutdown()
    finally:
        os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


def test_login_correto_zera_o_lockout(tmp_path):
    """Dono legítimo não fica preso: login CORRETO libera o IP na hora."""
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = "senha-secreta"
    try:
        limiter = ratelimit.RateLimiter(max_hits=3, window_s=300, block_base_s=60)
        httpd, base = _serve(tmp_path, login_limiter=limiter, auth=OwnerAuth())
        try:
            jar = CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            assert _post(base, "/api/login", {"password": "x"})[0] == 401
            assert _post(base, "/api/login", {"password": "x"})[0] == 401
            # 3ª tentativa (dentro do teto) com a senha certa → 200 e RESET do IP.
            assert _post(base, "/api/login", {"password": "senha-secreta"}, opener=op)[0] == 200
            # Contador zerado: mais tentativas erradas voltam a 401, não 429.
            assert _post(base, "/api/login", {"password": "x"})[0] == 401
            assert _post(base, "/api/login", {"password": "x"})[0] == 401
        finally:
            httpd.shutdown()
    finally:
        os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


# ── rota cara: teto de rajada por princípio (critério 2) ─────────────────────
def test_analyze_throttle_por_principio(tmp_path):
    """analyze é rota cara: N por janela por princípio (aqui o IP anônimo)."""
    limiter = ratelimit.RateLimiter(max_hits=3, window_s=60)
    httpd, base = _serve(tmp_path, expensive_limiter=limiter)
    try:
        # BYOK (X-LLM-Key) destrava o gate sem precisar de dono; a run roda no fake.
        hdr = {"X-LLM-Key": "sk-fake-byok"}
        codes = [_post(base, "/api/analyze", {"ticker": "NVDA"}, headers=hdr)[0]
                 for _ in range(3)]
        assert codes == [200, 200, 200]
        status, body, headers = _post(base, "/api/analyze", {"ticker": "NVDA"}, headers=hdr)
        assert status == 429 and body["error_code"] == "rate_limited"
        assert int(headers.get("Retry-After", "0")) >= 1
    finally:
        httpd.shutdown()


def test_setup123_nao_conta_como_rota_cara(tmp_path):
    """O atalho 1-2-3 (setup123, $0 de LLM) NÃO consome o teto de rajada."""
    limiter = ratelimit.RateLimiter(max_hits=2, window_s=60)
    httpd, base = _serve(tmp_path, expensive_limiter=limiter)
    try:
        # setup123 é estrutural: público, sem chave, e não deve ser barrado por rajada.
        for _ in range(5):
            st, _body, _h = _post(base, "/api/analyze",
                                  {"ticker": "NVDA", "method": "setup123"})
            assert st != 429  # nunca vira rate_limited
    finally:
        httpd.shutdown()


# ── teto de gasto: recusa explícita no estouro (critério 3) ──────────────────
def test_spend_cap_recusa_explicita_no_estouro(tmp_path, monkeypatch):
    """Estourado o teto diário, a rota cara na CHAVE DO SERVIDOR recusa com 402
    explícito (nunca degrada em silêncio); BYOK segue passando (é a chave dele)."""
    os.environ["TRADINGDEGENS_OWNER_TOKEN"] = "pw"
    try:
        guard = spend_guard.SpendGuard(cap_usd=1.0,
                                       ledger_path=str(tmp_path / "spend.json"))
        guard.record(2.0)  # já estourou o teto de US$1 hoje
        assert guard.exceeded() is True
        monkeypatch.setattr(spend_guard, "GUARD", guard)

        httpd, base = _serve(tmp_path, auth=OwnerAuth())
        try:
            jar = CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            assert _post(base, "/api/login", {"password": "pw"}, opener=op)[0] == 200
            # Dono, SEM BYOK → rodaria na chave do servidor → 402 recusa explícita.
            status, body, _ = _post(base, "/api/analyze", {"ticker": "NVDA"}, opener=op)
            assert status == 402 and body["error_code"] == "spend_cap"
            assert "teto de gasto" in body["error"].lower()
            # Mesmo dono, MAS com BYOK (chave própria) → não consome do teto → 200.
            status2, _b2, _ = _post(base, "/api/analyze", {"ticker": "NVDA"},
                                    opener=op, headers={"X-LLM-Key": "sk-fake"})
            assert status2 == 200
        finally:
            httpd.shutdown()
    finally:
        os.environ.pop("TRADINGDEGENS_OWNER_TOKEN", None)


# ── unidade: RateLimiter e SpendGuard (relógio falso, determinístico) ─────────
def test_ratelimiter_lockout_progressivo():
    t = {"now": 1000.0}
    lim = ratelimit.RateLimiter(max_hits=2, window_s=100, block_base_s=10,
                                block_max_s=40, clock=lambda: t["now"])
    assert lim.hit("k").allowed is True
    assert lim.hit("k").allowed is True
    d = lim.hit("k")  # estoura → bloqueio de 10s
    assert d.allowed is False and d.retry_after == 10
    t["now"] += 11  # passou o bloqueio
    assert lim.hit("k").allowed is True
    assert lim.hit("k").allowed is True
    d2 = lim.hit("k")  # 2ª reincidência → dobra pra 20s
    assert d2.allowed is False and d2.retry_after == 20


def test_ratelimiter_janela_desliza_sem_lockout():
    t = {"now": 0.0}
    lim = ratelimit.RateLimiter(max_hits=2, window_s=10, clock=lambda: t["now"])
    assert lim.hit("k").allowed is True
    assert lim.hit("k").allowed is True
    assert lim.hit("k").allowed is False  # 3ª barrada (sem lockout fixo)
    t["now"] += 11  # janela deslizou toda
    assert lim.hit("k").allowed is True


def test_spend_guard_desligado_por_default(tmp_path):
    g = spend_guard.SpendGuard(cap_usd=0.0, ledger_path=str(tmp_path / "l.json"))
    assert g.enabled() is False
    g.record(999.0)  # sem teto, gravar é irrelevante mas não pode explodir
    assert g.exceeded() is False


def test_spend_guard_persiste_e_estoura(tmp_path):
    path = str(tmp_path / "l.json")
    g = spend_guard.SpendGuard(cap_usd=5.0, ledger_path=path)
    g.record(3.0)
    assert g.exceeded() is False and g.remaining() == pytest.approx(2.0)
    g.record(3.0)
    assert g.exceeded() is True
    # Persistiu: outra instância lê o mesmo gasto do dia.
    g2 = spend_guard.SpendGuard(cap_usd=5.0, ledger_path=path)
    assert g2.spent_today() == pytest.approx(6.0) and g2.exceeded() is True
