"""``wit/desks/technicals.py`` — the RSI-on-a-zero-loss-window fix owed from
the Phase N2 audit (finding F2), wired up in Phase N7: a monotonic/constant
tape used to make ``_rsi`` return a bare NaN, which isn't valid JSON
(RFC 8259) and broke any non-Python reader of the journal.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from tests.conftest import make_bars
from wit.desks import technicals


def _monotonic_rising_bars(n: int = 150) -> pd.DataFrame:
    """Strictly increasing closes - zero losses anywhere in the RSI window,
    the exact condition that used to produce a NaN RSI."""
    close = np.linspace(1.10, 1.20, n)
    index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": close, "high": close + 0.0001, "low": close - 0.0001,
            "close": close, "tick_volume": np.full(n, 500),
        },
        index=index,
    )


def test_rsi_clamps_to_neutral_on_a_zero_loss_window():
    bars = _monotonic_rising_bars()
    t = technicals.compute("EURUSD", bars)
    assert t.rsi == 50.0
    assert not math.isnan(t.rsi)


def test_rsi_is_json_serializable_on_a_zero_loss_window():
    bars = _monotonic_rising_bars()
    t = technicals.compute("EURUSD", bars)
    # A bare NaN would previously round-trip through json.dumps as the
    # non-standard literal `NaN`, which json.loads (and jq, JSON.parse in
    # any other language) accepts leniently but which isn't valid JSON.
    # Assert it's actually the float 50.0 through a real encode/decode.
    encoded = json.dumps(t.to_dict())
    assert json.loads(encoded)["rsi"] == 50.0


def test_rsi_still_reports_a_real_value_on_a_normal_tape(flat_bars):
    t = technicals.compute("EURUSD", flat_bars)
    assert 0.0 <= t.rsi <= 100.0
    assert not math.isnan(t.rsi)


def test_rsi_is_unaffected_on_an_ordinary_random_walk():
    """The clamp only changes behaviour on the zero-loss edge case - an
    ordinary noisy tape (losses present) must be untouched."""
    bars = make_bars(n=600, drift=0.0005, seed=7)
    t = technicals.compute("EURUSD", bars)
    assert not math.isnan(t.rsi)
    assert t.rsi != 50.0  # a real random walk essentially never lands exactly on 50.0
