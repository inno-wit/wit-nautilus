"""Edge-case coverage for the Markov/GARCH desks, ported from
Wit-Hedge-fund/tests/test_signals.py (import paths only). The Phase N2 fixture-
equivalence gate (test_desks_equivalence.py) only snapshots three well-behaved
frames; these pin the error paths and invariants around them — short-history
raises, the size-multiplier clamp, the GARCH convergence/timeframe behavior —
so a later refactor that moves a threshold or a clamp bound doesn't slip
through green (see the Phase N2 audit, finding F3).
"""
from __future__ import annotations

import pytest

from tests.conftest import make_bars
from wit.config import CONFIG
from wit.desks import garch, markov
from wit.desks.contract import GarchSignal, MarkovSignal

# ── Markov desk ──────────────────────────────────────────────────────────

def test_markov_emits_contract(flat_bars):
    sig = markov.compute("EURUSD", flat_bars)
    assert isinstance(sig, MarkovSignal)
    assert sig.regime in ("Bull", "Bear", "Sideways")
    assert sig.direction in ("BULL", "BEAR", "NEUTRAL")
    assert -1.0 <= sig.signal <= 1.0
    assert 0.0 <= sig.confidence <= 1.0
    assert sig.to_dict()["symbol"] == "EURUSD"


def test_markov_probabilities_form_a_distribution(flat_bars):
    sig = markov.compute("EURUSD", flat_bars)
    total = sig.bull_prob + sig.bear_prob + sig.sideways_prob
    assert total == pytest.approx(1.0, abs=1e-3)


def test_markov_leans_bullish_on_an_uptrend(bull_bars):
    assert markov.compute("XAUUSD", bull_bars).signal > 0


def test_markov_leans_bearish_on_a_downtrend(bear_bars):
    assert markov.compute("XAUUSD", bear_bars).signal < 0


def test_markov_transition_matrix_rows_sum_to_one(flat_bars):
    regimes = markov.classify_regimes(flat_bars["close"])
    matrix = markov.transition_matrix(regimes)
    assert matrix.shape == (3, 3)
    assert matrix.sum(axis=1) == pytest.approx([1.0, 1.0, 1.0])


def test_markov_rejects_short_history():
    with pytest.raises(ValueError, match="bars to classify"):
        markov.compute("EURUSD", make_bars(n=15))


# ── GARCH desk ───────────────────────────────────────────────────────────

def test_garch_emits_contract(flat_bars):
    sig = garch.compute("EURUSD", flat_bars, timeframe="H1")
    assert isinstance(sig, GarchSignal)
    assert sig.vol_forecast > 0
    assert sig.vol_regime in ("calm", "normal", "storm")
    assert 0.0 <= sig.vol_percentile <= 1.0
    assert sig.to_dict()["symbol"] == "EURUSD"


def test_garch_size_multiplier_stays_within_clamp(flat_bars):
    sig = garch.compute("EURUSD", flat_bars, timeframe="H1")
    assert CONFIG.risk.size_multiplier_floor <= sig.size_multiplier <= CONFIG.risk.size_multiplier_cap


def test_garch_sizes_down_a_higher_vol_tape():
    calm = garch.compute("EURUSD", make_bars(vol=0.001, seed=3), timeframe="H1")
    wild = garch.compute("EURUSD", make_bars(vol=0.010, seed=3), timeframe="H1")
    assert wild.vol_forecast > calm.vol_forecast
    assert wild.size_multiplier <= calm.size_multiplier


def test_garch_rejects_short_history():
    with pytest.raises(ValueError, match="need >= 100 returns"):
        garch.compute("EURUSD", make_bars(n=50))


def test_garch_annualization_scales_with_timeframe(flat_bars):
    h1 = garch.compute("EURUSD", flat_bars, timeframe="H1")
    d1 = garch.compute("EURUSD", flat_bars, timeframe="D1")
    assert h1.vol_forecast > d1.vol_forecast


def test_garch_rejects_an_unrecognized_timeframe(flat_bars):
    """Phase N2 audit finding F1: a NautilusTrader BarType string (e.g.
    "1-HOUR-LAST") must not silently fall back to a 252-bars/year default —
    that produced a measured ~2x size_multiplier error with no journal trail."""
    with pytest.raises(ValueError, match="unrecognized timeframe"):
        garch.compute("EURUSD", flat_bars, timeframe="1-HOUR-LAST")
