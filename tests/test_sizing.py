"""Phase N4 gate: the consensus gate and sizing maths hold after the
SymbolSpec -> InstrumentSpec / AccountInfo -> AccountSnapshot / lots -> quantity
/ points -> price-unit spread changes. Ported from Wit-Hedge-fund's
tests/test_risk.py (consensus gate + sizing maths), tests/test_enhancements.py
(cooldown/margin/correlation/revalidate), and tests/test_phase10.py (adaptive
multipliers honored by build_plan) — gate ordering and blocked-reason
substrings are asserted identically; only fixture construction and the units
they're expressed in changed, per wit/risk/sizing.py's module docstring.

Safety-layer (SafetyMonitor) and orchestrator-level (_size_multipliers)
sections of those files are NOT ported here — that logic moves into
FundStateActor/WitStrategy in Phase N5/N6.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_bars
from wit.committee.contract import CommitteeDecision
from wit.config import RiskConfig
from wit.desks import garch, markov, technicals
from wit.risk.account import AccountSnapshot
from wit.risk.instrument_spec import InstrumentSpec
from wit.risk.sizing import build_plan, revalidate_plan

ACCOUNT = AccountSnapshot(equity=50_000, margin_free=50_000)

# IBKR-realistic EURUSD spec: minTick 0.00005 confirmed live in Phase N0. Uses
# a permissive min_quantity (matching the MT5 fixture's own volume_min=0.01
# lots being effectively negligible) so these tests exercise the gates they
# name rather than tripping the sizing-floor gate incidentally.
SPEC = InstrumentSpec(
    instrument_id="EUR/USD.IDEALPRO", price_increment=0.00005,
    min_quantity=1_000.0, quantity_step=1.0, max_quantity=None,
)


def decision(**over) -> CommitteeDecision:
    base = {"symbol": "EURUSD", "action": "BUY", "conviction": 0.6, "risk_rating": "medium",
                "rationale": "r", "key_risk": "k", "stop_atr_mult": 2.0, "reward_risk": 2.0}
    return CommitteeDecision(**{**base, **over})


@pytest.fixture
def desks():
    bars = make_bars(drift=0.001, seed=11)  # uptrend -> Markov leans long
    return (technicals.compute("EURUSD", bars),
            markov.compute("EURUSD", bars),
            garch.compute("EURUSD", bars, "H1"))


def plan_for(d, desks, **over):
    tech, mk, gk = desks
    kwargs = {"decision": d, "tech": tech, "mk": mk, "gk": gk, "account": ACCOUNT, "spec": SPEC,
                  "spread": 0.00005, "open_positions_total": 0, "open_positions_symbol": 0}
    kwargs.update(over)
    return build_plan(**kwargs)


# ── Consensus gate ───────────────────────────────────────────────────────

def test_hold_is_never_approved(desks):
    p = plan_for(decision(action="HOLD", conviction=0.0), desks)
    assert not p.approved
    assert "HOLD" in p.blocked_by[0]


def test_wide_spread_blocks_the_trade(desks):
    p = plan_for(decision(), desks, spread=0.005)  # ~0.45% of ~1.1 price
    assert not p.approved
    assert any("spread" in b for b in p.blocked_by)


def test_spread_as_percentage_of_price_blocks_a_low_priced_symbol(desks):
    """The sole spread gate is now pct-of-price (max_spread_points was
    dropped as an incoherent cross-instrument concept on IBKR — see
    instrument_spec.py). This pins that a fixed absolute spread still scales
    correctly across instrument price levels."""
    tech, mk, gk = desks
    aligned_mk = type(mk)(**{**mk.to_dict(), "signal": 0.8})
    equity_spec = InstrumentSpec(**{**SPEC.__dict__, "price_increment": 0.01})

    cheap = type(tech)(**{**tech.to_dict(), "last_close": 5.0})
    p = plan_for(decision(), (cheap, aligned_mk, gk), spec=equity_spec, spread=0.10)
    assert not p.approved
    assert any("spread" in b for b in p.blocked_by)

    # The identical absolute spread on a higher-priced symbol clears the cap.
    normal = type(tech)(**{**tech.to_dict(), "last_close": 500.0})
    ok = plan_for(decision(), (normal, aligned_mk, gk), spec=equity_spec, spread=0.10)
    assert ok.approved


def test_max_concurrent_positions_blocks_the_trade(desks):
    p = plan_for(decision(), desks, open_positions_total=3)
    assert not p.approved
    assert any("max concurrent" in b for b in p.blocked_by)


def test_existing_symbol_position_blocks_the_trade(desks):
    p = plan_for(decision(), desks, open_positions_symbol=1)
    assert not p.approved
    assert any("already holding" in b for b in p.blocked_by)


def test_opposed_markov_regime_vetoes_the_pm(desks):
    tech, mk, gk = desks
    opposed = type(mk)(**{**mk.to_dict(), "signal": -0.9})
    p = plan_for(decision(action="BUY"), (tech, opposed, gk))
    assert not p.approved
    assert any("Markov regime opposes" in b for b in p.blocked_by)


def test_aligned_markov_regime_allows_the_trade(desks):
    tech, mk, gk = desks
    aligned = type(mk)(**{**mk.to_dict(), "signal": 0.8})
    p = plan_for(decision(action="BUY"), (tech, aligned, gk))
    assert p.approved
    assert p.quantity > 0


def test_a_blocked_plan_carries_no_size(desks):
    p = plan_for(decision(), desks, spread=0.05)
    assert (p.quantity, p.action, p.risk_amount) == (0.0, "HOLD", 0.0)


# ── Sizing maths ─────────────────────────────────────────────────────────

def test_risk_stays_within_the_configured_cap(desks):
    tech, mk, gk = desks
    p = plan_for(decision(conviction=1.0), (tech, type(mk)(**{**mk.to_dict(), "signal": 0.8}), gk))
    assert p.approved
    # 0.5% base x GARCH multiplier (<= 2.0) x conviction (<= 1.0) => <= 1% of equity.
    assert p.risk_pct <= RiskConfig().risk_per_trade * RiskConfig().size_multiplier_cap * 1.05


def test_higher_conviction_sizes_larger(desks):
    tech, mk, gk = desks
    aligned = (tech, type(mk)(**{**mk.to_dict(), "signal": 0.8}), gk)
    low = plan_for(decision(conviction=0.3), aligned)
    high = plan_for(decision(conviction=0.9), aligned)
    assert high.quantity > low.quantity


def test_wider_stop_sizes_smaller(desks):
    tech, mk, gk = desks
    aligned = (tech, type(mk)(**{**mk.to_dict(), "signal": 0.8}), gk)
    tight = plan_for(decision(stop_atr_mult=1.0), aligned)
    wide = plan_for(decision(stop_atr_mult=5.0), aligned)
    assert wide.quantity < tight.quantity


def test_stops_and_targets_sit_on_the_correct_side(desks):
    tech, mk, gk = desks
    aligned = (tech, type(mk)(**{**mk.to_dict(), "signal": 0.0}), gk)
    buy = plan_for(decision(action="BUY"), aligned)
    sell = plan_for(decision(action="SELL"), aligned)
    assert buy.stop_loss < buy.entry < buy.take_profit
    assert sell.take_profit < sell.entry < sell.stop_loss


def test_reward_risk_ratio_is_honoured(desks):
    tech, mk, gk = desks
    aligned = (tech, type(mk)(**{**mk.to_dict(), "signal": 0.8}), gk)
    p = plan_for(decision(action="BUY", reward_risk=3.0), aligned)
    risk_dist = p.entry - p.stop_loss
    reward_dist = p.take_profit - p.entry
    assert reward_dist / risk_dist == pytest.approx(3.0, rel=0.02)


def test_tiny_risk_budget_is_blocked_rather_than_rounded_up(desks):
    tech, mk, gk = desks
    aligned = (tech, type(mk)(**{**mk.to_dict(), "signal": 0.8}), gk)
    tiny = AccountSnapshot(equity=20.0, margin_free=20.0)
    p = plan_for(decision(conviction=0.05), aligned, account=tiny)
    assert not p.approved
    assert any("below broker minimum" in b for b in p.blocked_by)


def test_quantity_is_snapped_to_the_broker_step():
    spec = InstrumentSpec(instrument_id="x", price_increment=0.00005,
                          min_quantity=0.01, quantity_step=0.01, max_quantity=100.0)
    assert spec.round_quantity(0.1234) == 0.12
    assert spec.round_quantity(1e-9) == 0.01      # clamped up to the minimum
    assert spec.round_quantity(500.0) == 100.0    # clamped down to the maximum


def test_broker_minimum_stop_distance_is_respected(desks):
    tech, mk, gk = desks
    aligned = (tech, type(mk)(**{**mk.to_dict(), "signal": 0.8}), gk)
    wide_spec = InstrumentSpec(**{**SPEC.__dict__, "min_stop_distance": 0.25})
    p = plan_for(decision(action="BUY", stop_atr_mult=0.5), aligned, spec=wide_spec)
    assert p.entry - p.stop_loss >= 0.25 * 0.999


# ── Post-exit cooldown ───────────────────────────────────────────────────

def test_cooldown_blocks_reentry(desks):
    p = plan_for(decision(), desks, in_cooldown=True)
    assert not p.approved
    assert any("cooldown" in b for b in p.blocked_by)


# ── Free-margin gate ─────────────────────────────────────────────────────

def test_margin_gate_blocks_when_insufficient(desks):
    p = plan_for(decision(), desks, margin_fn=lambda s, side, qty: 1e9)
    assert not p.approved
    assert any("free margin" in b for b in p.blocked_by)


def test_margin_gate_passes_when_sufficient(desks):
    p = plan_for(decision(), desks, margin_fn=lambda s, side, qty: 10.0)
    assert p.approved


# ── Correlation / concentration cap ──────────────────────────────────────

def test_correlated_group_at_cap_blocks(desks):
    p = plan_for(decision(symbol="NVDA"), desks, open_symbols=("MSFT", "AAPL"))
    assert not p.approved
    assert any("correlated group" in b for b in p.blocked_by)


def test_correlated_group_below_cap_allows(desks):
    p = plan_for(decision(symbol="NVDA"), desks, open_symbols=("MSFT",))
    assert p.approved


def test_uncorrelated_symbol_is_unaffected(desks):
    p = plan_for(decision(symbol="EURUSD"), desks, open_symbols=("NVDA", "MSFT"))
    assert p.approved   # EURUSD is in no group


# ── Pre-execution revalidation ───────────────────────────────────────────

LOOSE = RiskConfig(max_entry_slippage_pct=1.0)   # 100% — lets bracket/min-dist show through


def test_revalidate_accepts_a_fresh_plan(desks):
    p = plan_for(decision(), desks)
    assert p.approved
    assert revalidate_plan(p, p.entry, p.entry, SPEC, 0.00005) is None


def test_revalidate_rejects_price_drift(desks):
    p = plan_for(decision(), desks)
    drifted = p.entry * 1.01           # 1% > 0.2% cap
    assert "drifted" in revalidate_plan(p, drifted, drifted, SPEC, 0.00005)


def test_revalidate_rejects_wide_live_spread(desks):
    p = plan_for(decision(), desks)
    assert "spread" in revalidate_plan(p, p.entry, p.entry, SPEC, 0.005)


def test_revalidate_rejects_broken_bracket(desks):
    p = plan_for(decision(), desks)    # BUY
    above_tp = p.take_profit + 1.0
    assert "bracket" in revalidate_plan(p, above_tp, above_tp, SPEC, 0.00005, LOOSE)


def test_revalidate_rejects_inside_broker_min_distance(desks):
    p = plan_for(decision(), desks)
    # A broker min-distance well above any plausible SL/TP distance from price.
    tight = InstrumentSpec(**{**SPEC.__dict__, "min_stop_distance": 10.0})
    assert "minimum stop distance" in revalidate_plan(p, p.entry, p.entry, tight, 0.00005, LOOSE)


# ── Adaptive multipliers honored by build_plan ────────────────────────────

def test_drawdown_mult_shrinks_the_risk_budget(desks):
    tech, mk, gk = desks
    aligned = (tech, type(mk)(**{**mk.to_dict(), "signal": 0.8}), gk)
    full = plan_for(decision(), aligned, drawdown_mult=1.0)
    throttled = plan_for(decision(), aligned, drawdown_mult=0.5)
    assert throttled.detail["target_risk_amount"] == pytest.approx(
        full.detail["target_risk_amount"] * 0.5, abs=0.02)   # 2dp rounding
    assert throttled.quantity < full.quantity
    assert throttled.detail["drawdown_mult"] == 0.5


def test_kelly_mult_grows_the_risk_budget(desks):
    tech, mk, gk = desks
    aligned = (tech, type(mk)(**{**mk.to_dict(), "signal": 0.8}), gk)
    base = plan_for(decision(), aligned, kelly_mult=1.0)
    boosted = plan_for(decision(), aligned, kelly_mult=1.5)
    assert boosted.detail["target_risk_amount"] == pytest.approx(
        base.detail["target_risk_amount"] * 1.5, abs=0.02)   # 2dp rounding
    assert boosted.detail["kelly_mult"] == 1.5
