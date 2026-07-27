"""Pure-logic helpers in wit/nautilus/strategy.py that don't need a running
NautilusTrader kernel to test - unlike WitStrategy itself, which needs a real
Trader/BacktestEngine (see tests/test_strategy_backtest.py). `Bar`/`Instrument`
objects construct fine standalone via nautilus_trader's test_kit.
"""
from __future__ import annotations

import pandas as pd
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from wit.nautilus.strategy import _bars_to_frame


def _bar(bar_type, instrument, ts_ns: int, close: float) -> Bar:
    return Bar(
        bar_type=bar_type,
        open=instrument.make_price(close),
        high=instrument.make_price(close + 0.001),
        low=instrument.make_price(close - 0.001),
        close=instrument.make_price(close),
        volume=instrument.make_qty(1000),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


def test_bars_to_frame_has_the_expected_columns_and_dtype():
    instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD")
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    bars = [_bar(bar_type, instrument, i * 3_600_000_000_000, 1.10 + i * 0.001)
           for i in range(5)]

    frame = _bars_to_frame(bars)
    assert list(frame.columns) == ["open", "high", "low", "close", "tick_volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "UTC"
    assert len(frame) == 5


def test_bars_to_frame_sorts_by_ts_event_regardless_of_input_order():
    """Cache.bars()'s return order isn't documented (see the function's own
    docstring) - feed it deliberately out of order and confirm the frame
    comes back ascending."""
    instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD")
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    ordered = [_bar(bar_type, instrument, i * 3_600_000_000_000, 1.10 + i * 0.001)
              for i in range(5)]
    shuffled = [ordered[3], ordered[0], ordered[4], ordered[1], ordered[2]]

    frame = _bars_to_frame(shuffled)
    assert list(frame["close"]) == sorted(frame["close"])
    assert frame.index.is_monotonic_increasing


def test_bars_to_frame_close_values_round_trip():
    instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD")
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    bars = [_bar(bar_type, instrument, 0, 1.2345)]

    frame = _bars_to_frame(bars)
    assert frame["close"].iloc[0] == 1.2345
