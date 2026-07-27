"""The native Quant Analyst node (wit/desks/quant_analyst.py), ported from
Wit-Hedge-fund/tests/test_quant_analyst.py.

Packaging is deterministic and LLM-free — these assert the report's shape,
its prompt rendering, and the agreement synthesis, not any model behaviour.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_bars
from wit.desks import garch, markov, quant_analyst, technicals
from wit.desks.market_intel import MarketIntel
from wit.ops import dream


@pytest.fixture
def desks():
    bars = make_bars(drift=0.001)
    return (technicals.compute("EURUSD", bars),
            markov.compute("EURUSD", bars),
            garch.compute("EURUSD", bars, "H1"))


def test_report_carries_every_desk(desks):
    tech, mk, gk = desks
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk)
    assert report.symbol == "EURUSD" and report.timeframe == "H1"
    assert report.technicals is tech and report.markov is mk and report.garch is gk
    assert report.intel is None


def _with(dc, **overrides):
    """Return a copy of a frozen dataclass with fields overridden."""
    return dc.__class__(**{**dc.to_dict(), **overrides})


def test_agreement_aligned_bullish(desks):
    tech, mk, _ = desks
    tech, mk = _with(tech, trend="up"), _with(mk, direction="BULL")
    assert quant_analyst._agreement(tech, mk) == "aligned bull"


def test_agreement_aligned_bearish(desks):
    tech, mk, _ = desks
    tech, mk = _with(tech, trend="down"), _with(mk, direction="BEAR")
    assert quant_analyst._agreement(tech, mk) == "aligned bear"


def test_agreement_conflicted(desks):
    tech, mk, _ = desks
    tech, mk = _with(tech, trend="up"), _with(mk, direction="BEAR")
    assert quant_analyst._agreement(tech, mk) == "conflicted (technicals vs regime)"


def test_agreement_no_clear_alignment_when_either_side_is_neutral(desks):
    tech, mk, _ = desks
    tech, mk = _with(tech, trend="flat"), _with(mk, direction="BULL")
    assert quant_analyst._agreement(tech, mk) == "no clear alignment"


def test_to_dict_nests_every_desk(desks):
    tech, mk, gk = desks
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk)
    d = report.to_dict()
    assert {"markov", "garch", "technicals", "intel", "agreement"} <= d.keys()
    assert d["markov"] == mk.to_dict()
    assert d["garch"] == gk.to_dict()
    assert d["technicals"] == tech.to_dict()
    assert d["intel"] is None


def test_prompt_block_carries_every_desk_and_the_agreement_read(desks):
    tech, mk, gk = desks
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk)
    block = report.as_prompt_block()
    for heading in ("TECHNICAL DESK", "MARKOV REGIME DESK", "GARCH RISK DESK",
                    "QUANT ANALYST READ"):
        assert heading in block
    assert f"{tech.rsi:.1f}" in block
    assert mk.regime in block
    assert f"{gk.size_multiplier:.2f}x" in block
    assert report.agreement in block


def test_prompt_block_includes_market_intel_when_present(desks):
    tech, mk, gk = desks
    intel = MarketIntel(symbol="NVDA", is_equity=True, sector="Technology",
                        pe_ratio=31.4, headlines=["Chips rally on demand"])
    report = quant_analyst.compute("NVDA", "H1", tech, mk, gk, intel)
    block = report.as_prompt_block()
    assert "MARKET INTELLIGENCE DESK" in block
    assert "Technology" in block and "Chips rally on demand" in block


def test_prompt_block_omits_market_intel_when_empty(desks):
    tech, mk, gk = desks
    empty = MarketIntel(symbol="EURUSD", is_equity=False)  # yfinance/finnhub both failed
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk, empty)
    assert "MARKET INTELLIGENCE DESK" not in report.as_prompt_block()


def test_prompt_block_omits_market_intel_when_absent(desks):
    tech, mk, gk = desks
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk)
    assert "MARKET INTELLIGENCE DESK" not in report.as_prompt_block()


# ── Dream state ──────────────────────────────────────────────────────────

def _dream_state_with_a_lesson() -> dream.DreamState:
    return dream.DreamState(
        dream_id="d1", generated_at="2026-07-01T00:00:00", window_days=30,
        decisions_considered=10, trades_scored=8,
        lessons=[dream.Lesson("l1", "NVDA BUYs underperform", "symbol", "NVDA",
                              "medium", 8, 0.2, -5.0)],
    )


def test_to_dict_dream_defaults_to_none(desks):
    tech, mk, gk = desks
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk)
    assert report.to_dict()["dream"] is None


def test_prompt_block_includes_self_review_desk_when_dream_has_lessons(desks):
    tech, mk, gk = desks
    state = _dream_state_with_a_lesson()
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk, dream=state)
    block = report.as_prompt_block()
    assert "SELF-REVIEW DESK" in block
    assert "NVDA BUYs underperform" in block
    assert report.to_dict()["dream"] == state.to_dict()


def test_prompt_block_omits_self_review_desk_when_dream_has_no_lessons(desks):
    tech, mk, gk = desks
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk, dream=dream.DreamState.empty())
    assert "SELF-REVIEW DESK" not in report.as_prompt_block()
