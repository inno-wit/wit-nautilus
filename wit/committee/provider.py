"""The ``DecisionProvider`` protocol — what makes one strategy runnable in
backtest, paper, and live (build plan §1.2).

``decide`` is a plain synchronous method, not ``async def``. That is a
deliberate simplification versus the build plan's original draft (written
before Phase N0 confirmed how a NautilusTrader ``Strategy``/``Actor`` runs
off-loop work): ``run_in_executor``/``queue_for_executor`` take a regular
callable and dispatch it to a registered thread-pool executor. A synchronous
``anthropic.Anthropic`` client blocking a worker thread is exactly as safe as
an async client there — it never touches the event loop either way — so
``LiveCommitteeProvider`` stays a near-verbatim, lower-risk port of the MT5
build's ``Committee`` rather than an async rewrite with no off-loop benefit.

**The precondition isn't "live vs. backtest"** — the Phase N3 audit (finding
F8) traced this into ``nautilus_trader`` 1.230.0's actual source: the branch
is ``if self._executor is None: call inline`` (``actor.pyx``), and the
executor is registered exactly once, when the kernel starts
(``kernel.py``'s ``_register_executor()`` during ``start_async``). The
standard ``TradingNode``/``BacktestEngine`` paths always register one before
any strategy runs, which is why "live dispatches off-loop, backtest runs
inline" holds in practice — but an actor/strategy added to the trader
*after* the kernel has already started would never receive the registration,
and its ``run_in_executor`` calls would then run inline on the event-loop
thread regardless of live/backtest mode. Phase N5/N6 must add strategies
before ``node.build()``/kernel start, not after.

Because the executor is a shared thread pool (not one worker per strategy),
``LiveCommitteeProvider`` and ``ReplayCommitteeProvider`` must be safe to
call from multiple threads concurrently — see their own docstrings for how
each handles it (a locked rate limiter; a SQLite connection opened with
``check_same_thread=False`` plus a lock around every read/write).
See docs/BUILD_PLAN.md Phase N3.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from wit.committee.contract import CommitteeDecision
from wit.config import CONFIG, Config
from wit.desks.quant_analyst import QuantAnalystReport

if TYPE_CHECKING:
    from wit.committee.live import LiveCommitteeProvider
    from wit.committee.rules import RulePolicyProvider


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


_COMMITTEE_MODES = ("llm", "rules")


def build_committee_provider(cfg: Config = CONFIG) -> "LiveCommitteeProvider | RulePolicyProvider":
    """Construct the committee ``cfg.committee_mode`` selects (mirrors the MT5
    build's ``Orchestrator.committee`` property). Imports are deferred so a
    ``rules``-mode run never needs ``anthropic`` importable, or any LLM/
    NaraRouter key set, just to construct a provider.

    A typo'd ``WIT_COMMITTEE_MODE`` (e.g. ``"rule"``) must not silently fall
    through to the ``llm`` branch below — that would run the exact LLM
    committee the operator thought they'd turned off, with no warning
    anywhere. Fail fast instead, same posture as ``LiveCommitteeProvider``'s
    own half-configured-.env check."""
    if cfg.committee_mode not in _COMMITTEE_MODES:
        raise ValueError(
            f"WIT_COMMITTEE_MODE={cfg.committee_mode!r} not recognized "
            f"(expected one of {_COMMITTEE_MODES!r})"
        )
    if cfg.committee_mode == "rules":
        from wit.committee.rules import RulePolicyProvider
        return RulePolicyProvider()
    from wit.committee.live import LiveCommitteeProvider
    return LiveCommitteeProvider(llm=cfg.llm, timeframe=cfg.timeframe)
