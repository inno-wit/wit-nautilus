"""LLM committee: bull/bear researchers -> Portfolio Manager, behind a DecisionProvider.

``contract.py`` (the ``CommitteeDecision`` dataclass + ``distinctiveness()``) landed in
Phase N2, pulled forward from N3 because ``wit/ops/prefilter.py`` needs it to construct
its synthetic HOLDs. It has zero network/LLM dependencies.

Phase N3 — ``provider.py``/``live.py``/``replay.py``/``stub.py``/``prompts.py``: the actual
committee port, prompts ported verbatim from ``Wit-Hedge-fund/engine/agents_bridge.py``.
``DecisionProvider`` (live / replay / stub) is new: it's what makes the same strategy code
run in backtest, paper, and live. Defaults to direct Anthropic rather than a free-tier
gateway — see the build plan §Phase N3 for why.
"""
