"""The committee's decision contract — pure data, no LLM client.

Pulled forward from Phase N3 into N2: ``wit/ops/prefilter.py`` and
``wit/ops/market_hours.py`` both construct ``CommitteeDecision`` HOLDs for
symbols the committee never convenes on, so the contract has to exist before
either of those desks compiles. This module has zero network/LLM dependencies
(``provider.py``/``live.py``/``replay.py``/``stub.py``/``prompts.py`` — the
actual committee port — land in Phase N3 on top of this).

Ported verbatim from ``Wit-Hedge-fund/engine/agents_bridge.py``'s contract
section.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Action = Literal["BUY", "SELL", "HOLD"]

# Below this word-set distinctiveness the bull and bear cases are near-identical
# — the "rubber-stamp debate" where the two researchers echo each other and the
# PM gets no real adversarial check. Recorded on every decision so the journal
# (and the dream cycle) can see whether degenerate debates correlate with worse
# outcomes; diagnostic only, it does not itself block a trade.
DEBATE_DISTINCTIVENESS_FLOOR = 0.20
_WORD = re.compile(r"[a-z]{3,}")   # ignore numbers, tickers and 1-2 char tokens


def distinctiveness(bull: str, bear: str) -> float:
    """How different the two researcher cases are, in [0, 1] (1 = disjoint word
    sets, 0 = identical). Word-set Jaccard, lowercased, short tokens dropped.
    Empty input -> 1.0 (cannot judge, so do not false-flag)."""
    a = set(_WORD.findall(bull.lower()))
    b = set(_WORD.findall(bear.lower()))
    if not a or not b:
        return 1.0
    return round(1.0 - len(a & b) / len(a | b), 3)


@dataclass(frozen=True)
class CommitteeDecision:
    symbol: str
    action: Action
    conviction: float             # [0, 1] - scales position size downstream
    risk_rating: str              # "low" | "medium" | "high"
    rationale: str
    key_risk: str
    stop_atr_mult: float          # PM's preferred stop distance in ATRs
    reward_risk: float            # target R multiple
    bull_case: str = ""
    bear_case: str = ""
    model: str = ""            # the alias we *requested*
    served_model: str = ""     # the model actually served (msg.model); a gateway
                                # can silently substitute, so the audit trail
                                # records what really answered, not what we asked
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def abstain(cls, symbol: str, reason: str) -> CommitteeDecision:
        """A committee that cannot reach a verdict must not trade."""
        return cls(
            symbol=symbol, action="HOLD", conviction=0.0, risk_rating="high",
            rationale=f"Committee abstained: {reason}", key_risk="no decision reached",
            stop_atr_mult=2.0, reward_risk=1.5, error=reason,
        )
