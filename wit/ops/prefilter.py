"""Deterministic pre-committee gate — skip the LLM when there is no edge to weigh.

The committee is the fund's most expensive step: three LLM calls per symbol
(bull, bear, PM), paced against a rate limit. Most bars are HOLD. When the two
*directional* desks both see nothing — technical trend ``flat`` AND Markov
direction ``NEUTRAL`` — the researchers would be arguing from noise and the PM
overwhelmingly stands aside. This gate short-circuits exactly that state: it
journals a HOLD directly, spending zero LLM calls, producing the *same
outcome* the committee would have (a HOLD) at a fraction of the cost.

Two hard design rules keep this honest:

1. **Conservative by construction.** It only fires when *neither* directional
   desk has a lean. The gate can never suppress a symbol one of the desks
   flagged.
2. **Default OFF, validated on real history.** Enabling it changes which
   symbols reach the committee, so it is gated behind ``PrefilterConfig``
   (``WIT_PREFILTER``) and shipped with :func:`replay`, which re-runs the exact
   same predicate over the journal. You flip it on from that evidence, not
   from faith. See the MT5 build's own replay: currently NOT SAFE (3 XAUUSD
   trades would have been suppressed) — this port carries the gate OFF by
   default for the same reason; a fresh replay against this build's own
   journal is required before it is ever enabled here.

The live strategy and the replay harness call the *same* predicate
(:func:`_skip`), so the replay's numbers are exactly what production would do.

Ported verbatim from ``Wit-Hedge-fund/engine/prefilter.py`` (Phase N2).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from wit.committee.contract import CommitteeDecision
from wit.config import CONFIG, PrefilterConfig
from wit.desks.contract import MarkovSignal
from wit.desks.technicals import Technicals

_FLAT_TREND = "flat"
_NEUTRAL_DIRECTION = "NEUTRAL"


def _skip(trend: str, markov_direction: str) -> bool:
    """The single source of truth for the gate — used live and in replay."""
    return trend == _FLAT_TREND and markov_direction == _NEUTRAL_DIRECTION


def should_skip(
    tech: Technicals, mk: MarkovSignal, cfg: PrefilterConfig | None = None
) -> tuple[bool, str]:
    """Live decision: ``(skip?, human-readable reason)``."""
    cfg = cfg or CONFIG.prefilter
    if not cfg.enabled:
        return False, ""
    if _skip(tech.trend, mk.direction):
        return True, (
            f"no directional edge — technicals {tech.trend}, "
            f"Markov {mk.direction.lower()} (signal {mk.signal:+.3f})"
        )
    return False, ""


def synthetic_hold(symbol: str, reason: str) -> CommitteeDecision:
    """The HOLD a skipped symbol is journalled as. A clean, low-risk HOLD — not
    :meth:`CommitteeDecision.abstain`, which signals a *failure* to decide.
    ``detail.prefiltered`` marks it so the journal, reflection and replay can
    tell a gated HOLD from a committee one."""
    return CommitteeDecision(
        symbol=symbol,
        action="HOLD",
        conviction=0.0,
        risk_rating="low",
        rationale=f"Pre-filter: {reason}. Committee not convened (no LLM spend).",
        key_risk="none — no directional edge for the desks to act on",
        stop_atr_mult=2.0,
        reward_risk=1.5,
        model="prefilter",
        detail={"prefiltered": True},
    )


# ── Replay validation (offline, over the journal) ─────────────────────────


@dataclass
class ReplayReport:
    """What the gate *would* have done to already-journalled committee decisions.

    The number that governs the go/no-go decision is ``skipped_executed`` —
    real trades the gate would have prevented.
    """

    decisions: int = 0
    prefiltered_already: int = 0
    would_skip: int = 0
    skipped_hold: int = 0
    skipped_committee_nonhold: int = 0
    skipped_approved: int = 0
    skipped_executed: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)
    min_decisions_required: int = 0
    min_skips_required: int = 0

    @property
    def llm_calls_saved(self) -> int:
        return self.would_skip * 3

    @property
    def skip_rate(self) -> float:
        base = self.decisions - self.prefiltered_already
        return self.would_skip / base if base else 0.0

    @property
    def safe(self) -> bool:
        """True when the gate would not have blocked a single *approved* plan."""
        return self.skipped_approved == 0

    @property
    def evidence_sufficient(self) -> bool:
        base = self.decisions - self.prefiltered_already
        return (base >= self.min_decisions_required
                and self.would_skip >= self.min_skips_required)

    @property
    def enable_recommended(self) -> bool:
        return self.safe and self.evidence_sufficient

    def format(self) -> str:
        base = self.decisions - self.prefiltered_already
        lines = [
            "== Pre-filter replay (what the gate would have done to journalled history) ==",
            f"Committee decisions considered : {base}"
            + (f"  (+{self.prefiltered_already} already pre-filtered, excluded)"
               if self.prefiltered_already else ""),
            (f"Would skip (no directional edge): {self.would_skip}"
             f"  ({self.skip_rate:.0%} of committee decisions)"),
            f"  -> committee also said HOLD    : {self.skipped_hold}   (harmless — same outcome)",
            (f"  -> committee said BUY/SELL     : {self.skipped_committee_nonhold}"
             f"   (deliberation would be lost)"),
            (f"  -> plan was APPROVED           : {self.skipped_approved}"
             f"   (trade intent lost — what executes live)"),
            (f"  -> order was executed          : {self.skipped_executed}"
             f"   (real trade lost; ~0 if history is dry-run)"),
            f"LLM calls saved over this window : ~{self.llm_calls_saved}",
        ]
        if self.min_decisions_required or self.min_skips_required:
            lines.append(
                f"Evidence bar : {base}/{self.min_decisions_required} decisions, "
                f"{self.would_skip}/{self.min_skips_required} flat+neutral skips "
                f"-> {'MET' if self.evidence_sufficient else 'NOT MET'}"
            )
        lines.append("")
        if not self.safe:
            lines.append(
                f"VERDICT: NOT SAFE as-is — the gate would have suppressed "
                f"{self.skipped_approved} approved plan(s) the committee reached in a "
                f"flat+neutral state ({self.skipped_executed} of them executed live). "
                f"Review the examples before enabling.")
        elif not self.evidence_sufficient:
            lines.append(
                "VERDICT: INCONCLUSIVE — no approved trade would have been blocked, "
                "but the history is too thin to trust yet. Keep running the "
                "committee and re-check once the evidence bar above is met.")
        else:
            lines.append(
                "VERDICT: SAFE and sufficiently evidenced — the gate blocks no "
                "approved trade intent across enough history. Enable with "
                "WIT_PREFILTER=true.")
        if self.examples:
            lines.append("\nExamples of skipped-but-non-HOLD committee decisions:")
            for ex in self.examples[:8]:
                lines.append(
                    f"  {ex['symbol']:<8} committee={ex['committee_action']:<4} "
                    f"plan={ex['plan_action']:<4} approved={ex['approved']!s:<5} "
                    f"executed={ex['executed']}  trend={ex['trend']} markov={ex['markov']}"
                )
        return "\n".join(lines)


def replay(
    records: Iterable[dict[str, Any]], cfg: PrefilterConfig | None = None
) -> ReplayReport:
    """Re-run the gate over journalled ``decision`` records."""
    cfg = cfg or PrefilterConfig()
    rep = ReplayReport(
        min_decisions_required=cfg.min_replay_decisions,
        min_skips_required=cfg.min_observed_skips,
    )
    for rec in records:
        if rec.get("type") != "decision":
            continue
        quant = rec.get("quant") or {}
        tech = quant.get("technicals") or {}
        markov = quant.get("markov") or {}
        trend = tech.get("trend")
        direction = markov.get("direction")
        if trend is None or direction is None:
            continue

        rep.decisions += 1
        committee = rec.get("committee") or {}
        if committee.get("detail", {}).get("prefiltered"):
            rep.prefiltered_already += 1
            continue

        if not _skip(trend, direction):
            continue

        rep.would_skip += 1
        committee_action = committee.get("action", "?")
        plan = rec.get("plan") or {}
        plan_action = rec.get("action", "?")
        approved = bool(plan.get("approved"))
        executed = bool(rec.get("executed"))
        if committee_action == "HOLD":
            rep.skipped_hold += 1
        else:
            rep.skipped_committee_nonhold += 1
            rep.examples.append({
                "symbol": rec.get("symbol", "?"),
                "committee_action": committee_action,
                "plan_action": plan_action,
                "approved": approved,
                "executed": executed,
                "trend": trend,
                "markov": direction,
            })
        if approved:
            rep.skipped_approved += 1
        if executed:
            rep.skipped_executed += 1
    return rep
