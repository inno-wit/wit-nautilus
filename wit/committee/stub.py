"""A deterministic, no-network committee stand-in for tests, CI, and parameter
sweeps (build plan §1.2's ``StubPolicyProvider``). Mirrors the MT5 build's
``StubCommittee`` test double (``Wit-Hedge-fund/tests/test_orchestrator.py``),
promoted from a test fixture to a real, reusable ``DecisionProvider``
implementation since backtest sweeps need the same fixed-verdict behavior
outside of pytest.
"""
from __future__ import annotations

from wit.committee.contract import Action, CommitteeDecision
from wit.desks.quant_analyst import QuantAnalystReport


class StubPolicyProvider:
    """Always returns the same verdict, regardless of input. Implements
    ``DecisionProvider``."""

    def __init__(self, action: Action = "HOLD", conviction: float = 0.6):
        self.action = action
        self.conviction = conviction
        self.calls = 0

    def decide(
        self, report: QuantAnalystReport, *, instrument_id: str = "", bar_ts_ns: int = 0
    ) -> CommitteeDecision:
        self.calls += 1
        return CommitteeDecision(
            symbol=report.symbol,
            action=self.action,
            conviction=0.0 if self.action == "HOLD" else self.conviction,
            risk_rating="medium",
            rationale="stub",
            key_risk="stub",
            stop_atr_mult=2.0,
            reward_risk=2.0,
            model="stub",
        )
