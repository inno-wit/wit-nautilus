"""The ``DecisionProvider`` protocol — what makes one strategy runnable in
backtest, paper, and live (build plan §1.2).

``decide`` is a plain synchronous method, not ``async def``. That is a
deliberate simplification versus the build plan's original draft (written
before Phase N0 confirmed how a NautilusTrader ``Strategy``/``Actor`` runs
off-loop work): ``run_in_executor``/``queue_for_executor`` take a regular
callable and dispatch it to a registered thread-pool executor in live mode,
calling it directly (synchronously) in backtest. A synchronous
``anthropic.Anthropic`` client blocking a worker thread is exactly as safe as
an async client there — it never touches the event loop either way — so
``LiveCommitteeProvider`` stays a near-verbatim, lower-risk port of the MT5
build's ``Committee`` rather than an async rewrite with no off-loop benefit.
See docs/BUILD_PLAN.md Phase N3.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from wit.committee.contract import CommitteeDecision
from wit.desks.quant_analyst import QuantAnalystReport


@runtime_checkable
class DecisionProvider(Protocol):
    def decide(
        self, report: QuantAnalystReport, *, instrument_id: str = "", bar_ts_ns: int = 0
    ) -> CommitteeDecision:
        """Run the committee (or its stand-in) for one symbol's bar. Never
        raises — implementations must abstain (``CommitteeDecision.abstain``)
        on any internal failure rather than let an exception reach the
        strategy's deliberation callback.

        ``instrument_id``/``bar_ts_ns`` are optional and only meaningful to
        ``ReplayCommitteeProvider`` (they key its decision cache); live and
        stub providers ignore them."""
        ...
