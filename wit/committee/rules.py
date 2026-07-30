"""Deterministic rule-based committee — no LLM, no NaraRouter dependency.

Implements ``DecisionProvider`` (``wit/committee/provider.py``): ``RulePolicyProvider``
exposes the same ``decide(report, *, instrument_id="", bar_ts_ns=0) -> CommitteeDecision``
contract as ``LiveCommitteeProvider``/``StubPolicyProvider``/``ReplayCommitteeProvider``, so
callers never need to know which committee is running behind ``CONFIG.committee_mode`` (see
``wit/config.py`` and ``wit/committee/provider.py::build_committee_provider``). It also
defines ``.dream(...)``, which ``wit/ops/dream.py::run()`` and
``wit/nautilus/actor.py::_on_weekly_dream`` both duck-type via ``hasattr(committee, "dream")``
— rules mode gets the weekly self-review with no LLM call either.

The decision rule extends ``wit.desks.quant_analyst``'s ``agreement`` read (trade the Markov
lean past ``entry_threshold``) with two modifiers:

* ``agreement`` — technicals and the Markov regime confirming each other boosts conviction;
  disagreeing dampens it.
* GARCH ``vol_regime`` — a storm regime widens the stop and dampens conviction; calm
  tightens the stop. This is a different lever from GARCH's own ``size_multiplier`` (applied
  downstream in the risk engine), not double-counting the same effect.

Ported verbatim (logic unchanged) from ``Wit-Hedge-fund/engine/committee_rules.py`` —
deployed to the MT5 build's VPS 2026-07-30 after NaraRouter started silently 404ing there,
which made the LLM committee abstain to HOLD on every cycle with no visible error. Only the
imports differ: this build's ``CommitteeDecision``/desk contracts live under
``wit.committee``/``wit.desks`` instead of ``engine.agents_bridge``/``engine.signals``.
"""
from __future__ import annotations

from typing import Any

from wit.committee.contract import Action, CommitteeDecision
from wit.desks.contract import GarchSignal, MarkovSignal
from wit.desks.quant_analyst import QuantAnalystReport
from wit.desks.technicals import Technicals

_VOL_STOP_MULT = {"storm": 1.5, "calm": 0.85, "normal": 1.0}
_VOL_CONVICTION_MULT = {"storm": 0.8, "calm": 1.0, "normal": 1.0}

_MIN_STOP_ATR_MULT, _MAX_STOP_ATR_MULT = 0.5, 6.0
_MIN_REWARD_RISK, _MAX_REWARD_RISK = 0.5, 6.0

# Weekly dream-cycle lesson thresholds — deliberately generous so a lesson
# only fires on a real pattern, not sampling noise just above the
# min_bucket_trades floor wit.ops.dream already enforces before this ever runs.
_WEAK_WIN_RATE, _WEAK_AVG_PNL = 0.40, 0.0
_STRONG_WIN_RATE, _STRONG_AVG_PNL = 0.60, 0.0


def _agreement_multiplier(agreement: str, action: Action) -> float:
    """How much ``agreement`` should scale conviction for this ``action``.

    Only a genuine, direction-matching confirmation ("aligned bull" while
    buying) earns the boost; the opposite-direction "aligned" case and
    "conflicted" both mean the two desks aren't confirming the trade actually
    being taken, so they get the same treatment as ambiguity.
    """
    aligned_dir = "aligned bull" if action == "BUY" else "aligned bear"
    if agreement == aligned_dir:
        return 1.25
    if agreement.startswith("conflicted"):
        return 0.6
    return 0.85  # "no clear alignment", or the non-matching "aligned ..." case


def decide(
    symbol: str,
    tech: Technicals,
    mk: MarkovSignal,
    gk: GarchSignal,
    agreement: str,
    *,
    stop_atr_mult: float = 2.0,
    reward_risk: float = 2.0,
    entry_threshold: float = 0.1,
) -> CommitteeDecision:
    """Pure function: quant signals in, a full ``CommitteeDecision`` out.

    Never raises: any unexpected input (e.g. a NaN slipping through a desk)
    abstains to HOLD rather than crashing the cycle, exactly like the LLM
    committee's own failure path.
    """
    try:
        action: Action
        if mk.signal > entry_threshold:
            action = "BUY"
        elif mk.signal < -entry_threshold:
            action = "SELL"
        else:
            action = "HOLD"

        if action == "HOLD":
            conviction = 0.0
            agreement_mult = vol_conviction_mult = 1.0
        else:
            raw_conviction = min(1.0, abs(mk.signal))
            agreement_mult = _agreement_multiplier(agreement, action)
            vol_conviction_mult = _VOL_CONVICTION_MULT.get(gk.vol_regime, 1.0)
            conviction = max(0.0, min(1.0, raw_conviction * agreement_mult * vol_conviction_mult))

        vol_stop_mult = _VOL_STOP_MULT.get(gk.vol_regime, 1.0)
        applied_stop_atr_mult = max(_MIN_STOP_ATR_MULT,
                                    min(_MAX_STOP_ATR_MULT, stop_atr_mult * vol_stop_mult))
        applied_reward_risk = max(_MIN_REWARD_RISK, min(_MAX_REWARD_RISK, reward_risk))

        if gk.vol_regime == "storm" or conviction < 0.3:
            risk_rating = "high"
        elif conviction > 0.6:
            risk_rating = "low"
        else:
            risk_rating = "medium"

        rationale = (
            f"Rule engine: Markov signal {mk.signal:+.3f} ({mk.regime}, "
            f"confidence {mk.confidence:.2f}); technicals {tech.trend}; "
            f"{agreement}; vol regime {gk.vol_regime}."
        )
        if agreement.startswith("conflicted"):
            key_risk = ("technicals and Markov regime disagree — the regime read "
                        "may be lagging a trend change")
        elif gk.vol_regime == "storm":
            key_risk = ("elevated volatility regime — wider stop than usual, "
                        "expect larger adverse swings")
        else:
            key_risk = "regime reversal risk"

        bull_case = (f"Markov bull probability {mk.bull_prob:.0%}, "
                     f"signal {mk.signal:+.3f}, EMA trend {tech.trend}.")
        bear_case = (f"Markov bear probability {mk.bear_prob:.0%}, "
                     f"sideways probability {mk.sideways_prob:.0%}, RSI {tech.rsi:.1f}.")

        return CommitteeDecision(
            symbol=symbol, action=action, conviction=conviction, risk_rating=risk_rating,
            rationale=rationale, key_risk=key_risk,
            stop_atr_mult=applied_stop_atr_mult, reward_risk=applied_reward_risk,
            bull_case=bull_case, bear_case=bear_case,
            model="rule_engine", served_model="", error=None,
            detail={
                "raw_signal": mk.signal, "agreement": agreement, "vol_regime": gk.vol_regime,
                "agreement_mult": agreement_mult, "vol_conviction_mult": vol_conviction_mult,
                "vol_stop_mult": vol_stop_mult,
            },
        )
    except Exception as e:  # noqa: BLE001 - this path must never crash a cycle
        return CommitteeDecision.abstain(symbol, f"rule engine error: {e}")


def _lesson_confidence(trades: int, min_bucket_trades: int) -> str:
    if trades >= 3 * min_bucket_trades:
        return "high"
    if trades >= 1.5 * min_bucket_trades:
        return "medium"
    return "low"


class RulePolicyProvider:
    """No LLM, no API key, no NaraRouter — pure quant rules end to end.
    Implements ``DecisionProvider``."""

    def decide(
        self, report: QuantAnalystReport, *, instrument_id: str = "", bar_ts_ns: int = 0
    ) -> CommitteeDecision:
        return decide(report.symbol, report.technicals, report.markov, report.garch,
                      report.agreement)

    def dream(
        self, qualifying: dict[str, dict[str, dict]], scores: list[dict[str, Any]],
        window_days: int, min_bucket_trades: int,
    ) -> list[dict[str, Any]]:
        """Statistical stand-in for the weekly LLM self-review.

        Flags each qualifying bucket that's clearly underperforming or
        clearly strong against a fixed win-rate/avg-PnL bar. Every emitted
        dict names a ``(dimension, key)`` pair taken straight from
        ``qualifying``, so ``wit.ops.dream``'s own validation (drop anything
        naming a bucket it wasn't shown) always accepts them.
        """
        lessons: list[dict[str, Any]] = []
        for dimension, buckets in qualifying.items():
            for key, stats in buckets.items():
                trades = stats.get("trades", 0)
                win_rate = stats.get("win_rate", 0.0)
                avg_pnl = stats.get("avg_pnl", 0.0)
                confidence = _lesson_confidence(trades, min_bucket_trades)

                if win_rate < _WEAK_WIN_RATE or avg_pnl < _WEAK_AVG_PNL:
                    lessons.append({
                        "lesson": (f"{key} ({dimension}) underperforming: "
                                   f"{win_rate:.0%} win rate over {trades} trades, "
                                   f"avg PnL {avg_pnl:+.2f}."),
                        "dimension": dimension, "key": key, "confidence": confidence,
                    })
                elif win_rate > _STRONG_WIN_RATE and avg_pnl > _STRONG_AVG_PNL:
                    lessons.append({
                        "lesson": (f"{key} ({dimension}) performing well: "
                                   f"{win_rate:.0%} win rate over {trades} trades, "
                                   f"avg PnL {avg_pnl:+.2f}."),
                        "dimension": dimension, "key": key, "confidence": confidence,
                    })
        return lessons
