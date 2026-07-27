"""Pre-committee gate (wit/ops/prefilter.py), ported from
Wit-Hedge-fund/tests/test_phase9.py's prefilter section (predicate + replay
harness only — the orchestrator wiring tests move to Phase N5, since
``WitStrategy`` is what convenes the committee here, not an Orchestrator).

The gate is money-adjacent — it decides which symbols never reach the LLM — so
these pin both halves of its contract: it fires *only* on flat+neutral, and
the replay harness uses the *same* predicate the live path does.
"""
from __future__ import annotations

from wit.config import PrefilterConfig
from wit.desks.contract import MarkovSignal
from wit.desks.technicals import Technicals
from wit.ops import prefilter


def _tech(trend: str) -> Technicals:
    return Technicals(
        symbol="EURUSD", last_close=1.1, ema_fast=1.1, ema_slow=1.1, trend=trend,
        rsi=50.0, atr=0.001, atr_pct=0.0009, range_high=1.2, range_low=1.0,
        range_position=0.5, ret_20=0.0, ret_100=0.0,
    )


def _markov(direction: str, signal: float = 0.0) -> MarkovSignal:
    return MarkovSignal(
        symbol="EURUSD", regime="Sideways", direction=direction, signal=signal,
        confidence=0.2, bull_prob=0.33, bear_prob=0.33, sideways_prob=0.34,
        bars_used=400,
    )


# ── Predicate ─────────────────────────────────────────────────────────────

def test_skip_only_when_both_desks_are_neutral():
    assert prefilter._skip("flat", "NEUTRAL") is True
    assert prefilter._skip("up", "NEUTRAL") is False
    assert prefilter._skip("down", "NEUTRAL") is False
    assert prefilter._skip("flat", "BULL") is False
    assert prefilter._skip("flat", "BEAR") is False


def test_should_skip_respects_the_enabled_flag():
    tech, mk = _tech("flat"), _markov("NEUTRAL")
    off = PrefilterConfig(enabled=False)
    on = PrefilterConfig(enabled=True)
    assert prefilter.should_skip(tech, mk, off) == (False, "")
    skip, reason = prefilter.should_skip(tech, mk, on)
    assert skip is True and "no directional edge" in reason


def test_should_skip_lets_a_leaning_symbol_through_even_when_enabled():
    on = PrefilterConfig(enabled=True)
    skip, _ = prefilter.should_skip(_tech("up"), _markov("BULL", 0.4), on)
    assert skip is False


def test_synthetic_hold_is_a_clean_marked_hold():
    d = prefilter.synthetic_hold("EURUSD", "flat + neutral")
    assert d.action == "HOLD"
    assert d.conviction == 0.0
    assert d.error is None                       # a decision, not an abstention
    assert d.detail.get("prefiltered") is True
    assert d.model == "prefilter"


# ── Replay harness ────────────────────────────────────────────────────────

def _decision_rec(trend, direction, committee_action, plan_action, executed,
                  approved=None, prefiltered=False):
    approved = executed if approved is None else approved
    return {
        "type": "decision",
        "symbol": "EURUSD",
        "action": plan_action,
        "executed": executed,
        "committee": {"action": committee_action,
                      "detail": {"prefiltered": True} if prefiltered else {}},
        "plan": {"action": plan_action, "approved": approved, "blocked_by": []},
        "quant": {"technicals": {"trend": trend}, "markov": {"direction": direction}},
    }


def test_replay_counts_harmless_skips_as_safe():
    records = [
        _decision_rec("flat", "NEUTRAL", "HOLD", "HOLD", False),   # skip, harmless
        _decision_rec("flat", "NEUTRAL", "HOLD", "HOLD", False),   # skip, harmless
        _decision_rec("up", "BULL", "BUY", "BUY", True),           # not skipped
    ]
    rep = prefilter.replay(records)
    assert rep.decisions == 3
    assert rep.would_skip == 2
    assert rep.skipped_hold == 2
    assert rep.skipped_executed == 0
    assert rep.llm_calls_saved == 6
    assert rep.safe is True


def test_replay_flags_a_would_be_blocked_executed_trade_as_unsafe():
    records = [
        _decision_rec("flat", "NEUTRAL", "BUY", "BUY", True),      # gate would block a real trade
    ]
    rep = prefilter.replay(records)
    assert rep.would_skip == 1
    assert rep.skipped_committee_nonhold == 1
    assert rep.skipped_approved == 1
    assert rep.skipped_executed == 1
    assert rep.safe is False
    assert rep.examples and rep.examples[0]["committee_action"] == "BUY"


def test_replay_excludes_already_prefiltered_records():
    records = [_decision_rec("flat", "NEUTRAL", "HOLD", "HOLD", False, prefiltered=True)]
    rep = prefilter.replay(records)
    assert rep.prefiltered_already == 1
    assert rep.would_skip == 0


def test_replay_ignores_records_without_a_quant_trail():
    records = [{"type": "decision", "symbol": "X", "committee": {"action": "HOLD"}}]
    rep = prefilter.replay(records)
    assert rep.decisions == 0
