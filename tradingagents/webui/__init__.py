"""TradingDegens web interface.

A thin, dependency-free (stdlib ``http.server``) web front end that *drives*
the existing :class:`~tradingagents.graph.trading_graph.TradingAgentsGraph`
engine — it never reimplements analysis logic. Run it with::

    python -m tradingagents.webui

It binds ``0.0.0.0`` by default so it answers on the host's Tailscale IP, not
only ``127.0.0.1`` (see README: keep it on the Tailscale network, never expose
to the public internet — there is no authentication in this phase).
"""

from tradingagents.webui.runner import AnalysisRunner

__all__ = ["AnalysisRunner"]
