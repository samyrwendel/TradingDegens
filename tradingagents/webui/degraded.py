"""Normalização das entradas de ``degraded_sources`` na fronteira da webui.

O canal ``degraded_sources`` tem DOIS produtores no motor:

* :func:`tradingagents.graph.resilience.make_resilient_analyst` — a fonte caiu e
  a análise seguiu SEM ela (``kind="missing"``);
* :func:`tradingagents.agents.utils.debate_utils.degraded_entry` — o turno ESTÁ
  na análise, só teve o texto sinalizado pelo verificador (``kind="suspect"``).

Até o fix da task 20260828-003 o segundo produtor empurrava uma **string** solta
onde o contrato é um dict ``{label, report_key, reason, kind}``. O front lê
``d.label``/``d.reason``, então a string virava o placeholder "Análise feita SEM
a fonte: **fonte**" sem nenhum motivo listado.

O produtor foi corrigido na raiz, mas registros gravados antes do fix (e os
checkpoints retomados) ainda carregam a string — e o reuso de run copia esse
resultado pra frente. Este módulo é a casca de compatibilidade: toda entrada que
chega à UI passa por :func:`normalize_degraded` e sai no formato estruturado, com
a fonte NOMEADA. O fallback "fonte" do front vira o que devia ter sido desde o
começo: último recurso pra dado que não dá pra salvar.
"""

from __future__ import annotations

import re
from typing import Any

# "Bear Researcher (texto suspeito: severity=suspect invented=20 (1.73%) [...])"
#  ^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#      label                                  reason
_LEGACY_NOTE = re.compile(r"^\s*(?P<label>[^()]+?)\s*\((?P<reason>.+)\)\s*$", re.S)

_KINDS = ("missing", "suspect")


def _entry_from_text(text: str) -> dict[str, str]:
    """Recompose a structured entry from a legacy free-text note.

    Legacy notes only ever came from the debate/risk sanity guard, so the kind is
    ``"suspect"``: those turns SHIPPED — saying the analysis was made without
    them would be a lie. When the note doesn't parse, the whole text becomes the
    reason and the label stays empty (the UI's last-resort placeholder).
    """
    m = _LEGACY_NOTE.match(text)
    if not m:
        return {"label": "", "report_key": "", "reason": text.strip(), "kind": "suspect"}
    return {
        "label": m.group("label").strip(),
        "report_key": "",
        "reason": m.group("reason").strip(),
        "kind": "suspect",
    }


def normalize_degraded(items: Any) -> list[dict[str, str]]:
    """Coerce a ``degraded``/``degraded_sources`` list into the structured shape.

    Every returned entry has all four keys as strings. Unknown/absent ``kind``
    defaults to ``"missing"`` for dicts — the only dict producer before ``kind``
    existed was the resilience wrapper, whose sources really are absent.
    """
    if not isinstance(items, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "").strip().lower()
            entry = {
                "label": str(item.get("label") or "").strip(),
                "report_key": str(item.get("report_key") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
                "kind": kind if kind in _KINDS else "missing",
            }
        elif not item or not str(item).strip():
            # ``None``/``""``/``"   "``/``0`` carry no source and no reason —
            # listing them puts a bare "fonte" in the banner with nothing behind it.
            continue
        else:
            entry = _entry_from_text(item if isinstance(item, str) else str(item))
        out.append(entry)
    return out


def normalize_result(result: Any) -> Any:
    """Return ``result`` with its ``degraded`` list normalized in place.

    No-op for anything that isn't a dict carrying a non-empty ``degraded``, so it
    is safe to drop on every read path.
    """
    if isinstance(result, dict) and result.get("degraded"):
        result["degraded"] = normalize_degraded(result["degraded"])
    return result
