"""Adaptive sizing (wit/risk/adaptive.py): drawdown throttle + fractional
Kelly. Ported from Wit-Hedge-fund/tests/test_phase10.py's adaptive sections
(import paths only — pure math, nothing else changes).
"""
from __future__ import annotations

import pytest

from wit.config import AdaptiveConfig
from wit.risk import adaptive

# ── Drawdown throttle ────────────────────────────────────────────────────

def test_drawdown_multiplier_is_one_when_flat_or_green():
    assert adaptive.drawdown_multiplier(0.0, 50_000, 0.03, 0.5) == 1.0
    assert adaptive.drawdown_multiplier(200.0, 50_000, 0.03, 0.5) == 1.0


def test_drawdown_multiplier_scales_down_toward_the_floor():
    # Loss = exactly the daily cap (3% of start equity) -> floor.
    at_cap = adaptive.drawdown_multiplier(-1_500.0, 50_000, 0.03, 0.5)
    assert at_cap == pytest.approx(0.5, abs=1e-3)
    # Half the cap -> about halfway between 1.0 and the floor.
    half = adaptive.drawdown_multiplier(-750.0, 50_000, 0.03, 0.5)
    assert 0.5 < half < 1.0


def test_drawdown_multiplier_never_exceeds_one_past_the_cap():
    assert adaptive.drawdown_multiplier(-5_000.0, 50_000, 0.03, 0.5) == 0.5


def test_drawdown_multiplier_disabled_is_one():
    assert adaptive.drawdown_multiplier(-1_500.0, 50_000, 0.03, 0.5, enabled=False) == 1.0


# ── Fractional Kelly ─────────────────────────────────────────────────────

def test_kelly_stats_splits_wins_and_losses():
    s = adaptive.kelly_stats([100.0, -50.0, 60.0, 0.0, -40.0])
    assert s.wins == 2 and s.losses == 2      # break-even (0.0) counts as neither
    assert s.avg_win == pytest.approx(80.0)
    assert s.avg_loss == pytest.approx(45.0)


KCFG = AdaptiveConfig(use_fractional_kelly=True, kelly_min_trades=5,
                      kelly_fraction=0.25, kelly_mult_floor=0.5, kelly_mult_cap=1.5)


def test_kelly_multiplier_disabled_is_one():
    off = AdaptiveConfig(use_fractional_kelly=False)
    s = adaptive.kelly_stats([100.0] * 10 + [-10.0] * 2)
    assert adaptive.kelly_multiplier(s, 0.005, off) == 1.0


def test_kelly_multiplier_undersampled_is_one():
    s = adaptive.kelly_stats([100.0, -50.0])   # 2 trades < kelly_min_trades
    assert adaptive.kelly_multiplier(s, 0.005, KCFG) == 1.0


def test_kelly_multiplier_positive_edge_lifts_size_to_the_cap():
    # Strong edge; fractional Kelly dwarfs a 0.5% base, so it clamps to the cap.
    s = adaptive.kelly_stats([100.0] * 6 + [-50.0] * 4)   # p=0.6, b=2
    assert adaptive.kelly_multiplier(s, 0.005, KCFG) == KCFG.kelly_mult_cap


def test_kelly_multiplier_no_edge_falls_to_the_floor():
    s = adaptive.kelly_stats([50.0] * 4 + [-50.0] * 6)    # negative f* -> floor
    assert adaptive.kelly_multiplier(s, 0.005, KCFG) == KCFG.kelly_mult_floor
