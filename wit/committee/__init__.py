"""LLM committee: bull/bear researchers -> Portfolio Manager, behind a DecisionProvider.

Phase N3 — ``CommitteeDecision`` contract and prompts ported verbatim from
``Wit-Hedge-fund/engine/agents_bridge.py``. ``DecisionProvider`` (live / replay / stub) is new:
it's what makes the same strategy code run in backtest, paper, and live. Defaults to direct
Anthropic rather than a free-tier gateway — see the build plan §Phase N3 for why.
"""
