"""Direct coverage for wit/committee/contract.py, pulled forward into Phase N2
alongside its tests (Phase N2 audit finding F4: the module landed without
them). Ported from Wit-Hedge-fund/tests/test_committee.py and
tests/test_phase10.py's distinctiveness section.
"""
from __future__ import annotations

from wit.committee.contract import (
    DEBATE_DISTINCTIVENESS_FLOOR,
    CommitteeDecision,
    distinctiveness,
)


def test_abstain_helper_is_safe():
    d = CommitteeDecision.abstain("XAUUSD", "kill switch")
    assert (d.action, d.conviction) == ("HOLD", 0.0)
    assert d.error == "kill switch"
    assert d.risk_rating == "high"


def test_distinctiveness_is_zero_for_identical_cases():
    text = "The trend supports a long entry above resistance with tight risk."
    assert distinctiveness(text, text) == 0.0


def test_distinctiveness_is_high_for_disjoint_cases():
    d = distinctiveness("bullish breakout momentum strong",
                        "bearish rejection weakness fading")
    assert d > 0.8


def test_distinctiveness_empty_input_does_not_false_flag():
    assert distinctiveness("", "anything here") == 1.0


def test_healthy_opposing_cases_sit_above_the_floor():
    bull = ("Price reclaimed the 50 EMA and momentum is turning up; a long above "
            "resistance targets the prior swing high with a stop under support.")
    bear = ("Volume is fading into the rally and the daily trend is still down; a "
            "short into resistance targets a retest of the range low.")
    assert distinctiveness(bull, bear) >= DEBATE_DISTINCTIVENESS_FLOOR
