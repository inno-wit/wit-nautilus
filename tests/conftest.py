"""Shared test fixtures.

``make_bars`` is byte-for-byte the same generator as the MT5 build's
``Wit-Hedge-fund/tests/conftest.py`` (same seed/params) — it's what produced
``tests/fixtures/mt5_desks.json`` for the Phase N2 equivalence gate, and it lets
the rest of this suite synthesize OHLCV without a broker connection.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def make_bars(
    n: int = 600,
    drift: float = 0.0,
    vol: float = 0.002,
    seed: int = 7,
    start: float = 1.10,
) -> pd.DataFrame:
    """Geometric random walk shaped like an OHLCV frame."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(drift, vol, n)
    close = start * np.exp(np.cumsum(returns))
    noise = np.abs(rng.normal(0, vol / 2, n)) * close
    index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": np.r_[start, close[:-1]],
            "high": close + noise,
            "low": close - noise,
            "close": close,
            "tick_volume": rng.integers(100, 1000, n),
        },
        index=index,
    )


@pytest.fixture
def flat_bars() -> pd.DataFrame:
    return make_bars(drift=0.0)


@pytest.fixture
def bull_bars() -> pd.DataFrame:
    return make_bars(drift=0.0015, vol=0.002)


@pytest.fixture
def bear_bars() -> pd.DataFrame:
    return make_bars(drift=-0.0015, vol=0.002)
