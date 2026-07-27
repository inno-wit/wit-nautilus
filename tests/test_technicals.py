"""``wit/desks/technicals.py`` — the RSI-on-a-zero-loss-window fix owed from
the Phase N2 audit (finding F2), wired up in Phase N7: a monotonic/constant
tape used to make ``_rsi`` return a bare NaN, which isn't valid JSON
(RFC 8259) and broke any non-Python reader of the journal.

The clamp value itself was corrected by the Phase N7 audit's finding M2:
a monotonically *rising* tape (no losses, real gains) is Wilder's actual
100.0 - the mirror image of a pure downtrend, which already correctly
returned 0.0 - not a "neutral" 50.0. Only a truly flat tape (no gains and
no losses) has no direction to report, and that's the genuine 50.0 case.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from tests.conftest import make_bars
from wit.desks import technicals


def _straight_line_bars(n: int, start: float, stop: float) -> pd.DataFrame:
    close = np.linspace(start, stop, n)
    index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": close, "high": close + 0.0001, "low": close - 0.0001,
            "close": close, "tick_volume": np.full(n, 500),
        },
        index=index,
    )


def _monotonic_rising_bars(n: int = 150) -> pd.DataFrame:
    """Strictly increasing closes - zero losses anywhere in the RSI window,
    the exact condition that used to produce a NaN RSI."""
    return _straight_line_bars(n, 1.10, 1.20)


def _monotonic_falling_bars(n: int = 150) -> pd.DataFrame:
    """Strictly decreasing closes - the symmetric mirror of the rising
    case: zero gains, real losses."""
    return _straight_line_bars(n, 1.20, 1.10)


def _flat_bars(n: int = 150) -> pd.DataFrame:
    """A genuinely constant tape - zero gains AND zero losses. No
    direction to report at all, unlike the rising/falling cases."""
    close = np.full(n, 1.15)
    index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close,
         "tick_volume": np.full(n, 500)},
        index=index,
    )


def test_rsi_is_100_on_a_pure_uptrend():
    t = technicals.compute("EURUSD", _monotonic_rising_bars())
    assert t.rsi == 100.0
    assert not math.isnan(t.rsi)


def test_rsi_is_0_on_a_pure_downtrend():
    """The symmetric mirror of the uptrend case - always worked correctly
    (it never hit the zero-loss branch), pinned here so the two extremes
    are verified together."""
    t = technicals.compute("EURUSD", _monotonic_falling_bars())
    assert t.rsi == 0.0


def test_rsi_is_neutral_on_a_genuinely_flat_tape():
    t = technicals.compute("EURUSD", _flat_bars())
    assert t.rsi == 50.0


def test_rsi_is_json_serializable_on_a_zero_loss_window():
    bars = _monotonic_rising_bars()
    t = technicals.compute("EURUSD", bars)
    # A bare NaN would previously round-trip through json.dumps as the
    # non-standard literal `NaN`, which json.loads (and jq, JSON.parse in
    # any other language) accepts leniently but which isn't valid JSON.
    # Assert it's actually a real float through a real encode/decode.
    encoded = json.dumps(t.to_dict())
    assert json.loads(encoded)["rsi"] == 100.0


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
    assert t.rsi not in (0.0, 50.0, 100.0)  # a real random walk essentially never lands exactly on one
