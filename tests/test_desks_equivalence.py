"""Phase N2 gate: byte-identical desk output vs. the MT5 build.

``tests/fixtures/mt5_desks.json`` was captured by running the MT5 repo's own
``engine.signals.{technicals,markov,garch}`` (unmodified, read-only) over the
identical ``make_bars`` generator used here. If this test ever fails, the port
changed what question the committee is being asked and every downstream
comparison against the MT5 build is invalid — see docs/BUILD_PLAN.md Phase N2.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import make_bars
from wit.desks import garch, markov, technicals

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "mt5_desks.json").read_text(encoding="utf-8")
)

CASES = {
    "flat": make_bars(drift=0.0),
    "bull": make_bars(drift=0.0015, vol=0.002),
    "bear": make_bars(drift=-0.0015, vol=0.002),
}


@pytest.mark.parametrize("case", ["flat", "bull", "bear"])
def test_technicals_matches_mt5_build(case):
    bars = CASES[case]
    tech = technicals.compute("EURUSD", bars)
    assert tech.to_dict() == FIXTURES[case]["technicals"]
    assert tech.as_prompt_block() == FIXTURES[case]["technicals_prompt"]


@pytest.mark.parametrize("case", ["flat", "bull", "bear"])
def test_markov_matches_mt5_build(case):
    bars = CASES[case]
    mk = markov.compute("EURUSD", bars)
    assert mk.to_dict() == FIXTURES[case]["markov"]


@pytest.mark.parametrize("case", ["flat", "bull", "bear"])
def test_garch_matches_mt5_build(case):
    bars = CASES[case]
    gk = garch.compute("EURUSD", bars, timeframe="H1")
    assert gk.to_dict() == FIXTURES[case]["garch"]
