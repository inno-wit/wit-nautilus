"""Verification for the deterministic rule-based committee (no LLM).

Mirrors the guarantees ``tests/test_committee_live.py`` holds the LLM committee to
(HOLD never carries size, decisions never raise) plus the rule-specific behaviour:
agreement/vol-regime modifiers, threshold dead-bands, and the dream-cycle statistical
lesson generator. Ported from ``Wit-Hedge-fund/tests/test_committee_rules.py``.
"""
from __future__ import annotations

from tests.conftest import make_bars
from wit.committee.rules import RulePolicyProvider, decide
from wit.desks import garch, markov, quant_analyst, technicals
from wit.desks.contract import GarchSignal, MarkovSignal
from wit.desks.technicals import Technicals


def _tech(**over) -> Technicals:
    base = dict(symbol="EURUSD", last_close=1.10, ema_fast=1.101, ema_slow=1.10,
                trend="flat", rsi=50.0, atr=0.001, atr_pct=0.001,
                range_high=1.12, range_low=1.08, range_position=0.5,
                ret_20=0.0, ret_100=0.0)
    return Technicals(**{**base, **over})


def _mk(**over) -> MarkovSignal:
    base = dict(symbol="EURUSD", regime="Sideways", direction="NEUTRAL", signal=0.0,
                confidence=0.5, bull_prob=0.33, bear_prob=0.33, sideways_prob=0.34,
                bars_used=750)
    return MarkovSignal(**{**base, **over})


def _gk(**over) -> GarchSignal:
    base = dict(symbol="EURUSD", vol_forecast=0.1, vol_regime="normal",
                size_multiplier=1.0, realized_vol=0.1, vol_percentile=0.5, bars_used=750)
    return GarchSignal(**{**base, **over})


# ── Directional rule + agreement modifier ────────────────────────────────

def test_signal_past_threshold_buys():
    d = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(), "no clear alignment")
    assert d.action == "BUY"
    assert d.conviction > 0.0


def test_signal_past_negative_threshold_sells():
    d = decide("EURUSD", _tech(), _mk(signal=-0.5), _gk(), "no clear alignment")
    assert d.action == "SELL"
    assert d.conviction > 0.0


def test_signal_within_dead_band_holds():
    d = decide("EURUSD", _tech(), _mk(signal=0.05), _gk(), "aligned bull")
    assert d.action == "HOLD"
    assert d.conviction == 0.0


def test_aligned_bull_boosts_conviction_over_baseline():
    baseline = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(), "no clear alignment")
    aligned = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(), "aligned bull")
    assert aligned.action == baseline.action == "BUY"
    assert aligned.conviction > baseline.conviction


def test_aligned_bear_boosts_conviction_over_baseline():
    baseline = decide("EURUSD", _tech(), _mk(signal=-0.5), _gk(), "no clear alignment")
    aligned = decide("EURUSD", _tech(), _mk(signal=-0.5), _gk(), "aligned bear")
    assert aligned.action == baseline.action == "SELL"
    assert aligned.conviction > baseline.conviction


def test_conflicted_dampens_conviction_but_still_trades():
    baseline = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(), "no clear alignment")
    conflicted = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(),
                        "conflicted (technicals vs regime)")
    assert conflicted.action == "BUY"
    assert conflicted.conviction < baseline.conviction


def test_neutral_markov_direction_does_not_gate_a_signal_past_threshold():
    """direction is a coarser dead-band than entry_threshold on signal — a
    signal that clears entry_threshold must still trade even if direction
    itself reads NEUTRAL."""
    d = decide("EURUSD", _tech(), _mk(signal=0.15, direction="NEUTRAL"), _gk(),
               "no clear alignment")
    assert d.action == "BUY"


def test_signal_at_threshold_holds_regardless_of_direction():
    d = decide("EURUSD", _tech(), _mk(signal=0.1, direction="BULL"), _gk(),
               "aligned bull")
    assert d.action == "HOLD"


# ── GARCH vol-regime modifier ─────────────────────────────────────────────

def test_storm_widens_stop_and_dampens_conviction():
    normal = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(vol_regime="normal"),
                    "no clear alignment")
    storm = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(vol_regime="storm"),
                   "no clear alignment")
    assert storm.stop_atr_mult > normal.stop_atr_mult
    assert storm.conviction < normal.conviction
    assert storm.risk_rating == "high"


def test_calm_tightens_stop_versus_normal():
    normal = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(vol_regime="normal"),
                    "no clear alignment")
    calm = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(vol_regime="calm"),
                  "no clear alignment")
    assert calm.stop_atr_mult < normal.stop_atr_mult


# ── Invariants ─────────────────────────────────────────────────────────────

def test_hold_always_carries_zero_conviction():
    for signal in (-0.05, 0.0, 0.05, 0.1, -0.1):
        d = decide("EURUSD", _tech(), _mk(signal=signal), _gk(), "aligned bull")
        if d.action == "HOLD":
            assert d.conviction == 0.0


def test_stop_atr_mult_and_reward_risk_stay_within_bounds():
    d = decide("EURUSD", _tech(), _mk(signal=0.9), _gk(vol_regime="storm"),
               "aligned bull", stop_atr_mult=5.0, reward_risk=10.0)
    assert 0.5 <= d.stop_atr_mult <= 6.0
    assert 0.5 <= d.reward_risk <= 6.0

    d2 = decide("EURUSD", _tech(), _mk(signal=0.9), _gk(vol_regime="calm"),
               "aligned bull", stop_atr_mult=0.5, reward_risk=0.1)
    assert 0.5 <= d2.stop_atr_mult <= 6.0
    assert 0.5 <= d2.reward_risk <= 6.0


def test_decision_provenance_and_narrative():
    d = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(), "aligned bull")
    assert d.model == "rule_engine"
    assert d.served_model == ""
    assert d.error is None
    assert d.rationale and d.key_risk and d.bull_case and d.bear_case


def test_decide_never_raises_on_bad_vol_regime():
    """An unrecognized vol_regime string must fall through the multiplier
    dict lookups' defaults rather than raise."""
    d = decide("EURUSD", _tech(), _mk(signal=0.5), _gk(vol_regime="unknown"),
               "no clear alignment")
    assert d.error is None
    assert d.action == "BUY"


# ── RulePolicyProvider.decide() over real desks (DecisionProvider contract) ──

def _report():
    bars = make_bars(drift=0.0015, seed=7)
    tech = technicals.compute("EURUSD", bars)
    mk = markov.compute("EURUSD", bars)
    gk = garch.compute("EURUSD", bars, "H1")
    return quant_analyst.compute("EURUSD", "H1", tech, mk, gk)


def test_rule_provider_decide_end_to_end():
    d = RulePolicyProvider().decide(_report())
    assert d.model == "rule_engine"
    assert d.error is None
    assert d.rationale and d.key_risk and d.bull_case and d.bear_case
    assert d.action in ("BUY", "SELL", "HOLD")


def test_rule_provider_ignores_replay_only_kwargs():
    d = RulePolicyProvider().decide(_report(), instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=123)
    assert d.action in ("BUY", "SELL", "HOLD")


# ── Dream self-review (weekly, LLM-free) ──────────────────────────────────

def _bucket(trades, win_rate, avg_pnl):
    return {"trades": trades, "win_rate": win_rate,
            "avg_pnl": avg_pnl, "total_pnl": avg_pnl * trades}


def test_dream_flags_a_weak_bucket():
    qualifying = {"symbol": {"XAUUSD": _bucket(10, 0.2, -5.0)}}
    lessons = RulePolicyProvider().dream(qualifying, [], window_days=30, min_bucket_trades=5)
    assert len(lessons) == 1
    assert lessons[0]["dimension"] == "symbol"
    assert lessons[0]["key"] == "XAUUSD"
    assert "underperforming" in lessons[0]["lesson"]


def test_dream_flags_a_strong_bucket():
    qualifying = {"markov_regime": {"Bull": _bucket(12, 0.75, 8.0)}}
    lessons = RulePolicyProvider().dream(qualifying, [], window_days=30, min_bucket_trades=5)
    assert len(lessons) == 1
    assert "performing well" in lessons[0]["lesson"]


def test_dream_is_silent_on_a_middling_bucket():
    qualifying = {"symbol": {"EURUSD": _bucket(10, 0.5, 1.0)}}
    lessons = RulePolicyProvider().dream(qualifying, [], window_days=30, min_bucket_trades=5)
    assert lessons == []


def test_dream_confidence_scales_with_trade_count():
    qualifying = {
        "symbol": {
            "LOW": _bucket(5, 0.2, -1.0),      # == min_bucket_trades -> low
            "MED": _bucket(8, 0.2, -1.0),      # >= 1.5x -> medium
            "HIGH": _bucket(16, 0.2, -1.0),    # >= 3x -> high
        }
    }
    lessons = {l["key"]: l for l in RulePolicyProvider().dream(
        qualifying, [], window_days=30, min_bucket_trades=5)}
    assert lessons["LOW"]["confidence"] == "low"
    assert lessons["MED"]["confidence"] == "medium"
    assert lessons["HIGH"]["confidence"] == "high"


def test_dream_empty_qualifying_yields_no_lessons():
    assert RulePolicyProvider().dream({}, [], window_days=30, min_bucket_trades=5) == []


def test_dream_never_names_a_bucket_outside_qualifying():
    qualifying = {"vol_regime": {"storm": _bucket(9, 0.1, -3.0)}}
    lessons = RulePolicyProvider().dream(qualifying, [], window_days=30, min_bucket_trades=5)
    for l in lessons:
        assert l["key"] in qualifying[l["dimension"]]
