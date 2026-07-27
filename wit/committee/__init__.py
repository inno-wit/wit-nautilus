"""LLM committee: bull/bear researchers -> Portfolio Manager, behind a DecisionProvider.

``contract.py`` (the ``CommitteeDecision`` dataclass + ``distinctiveness()``) landed in
Phase N2, pulled forward from N3 because ``wit/ops/prefilter.py`` needs it to construct
its synthetic HOLDs. It has zero network/LLM dependencies.

Phase N3: ``provider.py`` (the ``DecisionProvider`` protocol — one ``decide()`` method,
synchronous by design, see that module's docstring), ``prompts.py`` (bull/bear/PM prompts
+ tool schemas, ported from ``Wit-Hedge-fund/engine/agents_bridge.py``), ``live.py``
(``LiveCommitteeProvider`` — near-verbatim port of that file's ``Committee``, defaults to
direct Anthropic rather than a free-tier gateway), ``stub.py`` (``StubPolicyProvider`` — a
fixed-verdict provider for tests/CI/sweeps), ``replay.py`` (``ReplayCommitteeProvider`` — a
new SQLite decision cache, ``record``/``strict`` modes, what makes a full-fidelity backtest
of an LLM-mediated strategy affordable to run more than once).
"""
