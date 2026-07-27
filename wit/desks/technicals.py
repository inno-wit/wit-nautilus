"""Deterministic technical digest — the "market analyst desk".

Indicators are computed in-process rather than asked of an LLM: it is free,
reproducible, and gives the committee hard numbers to argue over instead of
hallucinated ones. The digest is also what the risk engine reads ATR from.

Ported verbatim from ``Wit-Hedge-fund/engine/signals/technicals.py`` (Phase N2).
Gate: byte-identical ``Technicals``/``as_prompt_block()`` output vs. the MT5
build on the same input DataFrame - with one deliberate divergence, added in
Phase N7: the MT5 build's RSI returns a bare NaN on a zero-loss window
(never actually reached JSON there), which this build clamps to a real
Wilder-RSI value (100.0 rising / 50.0 flat - see ``_rsi``'s own docstring)
instead, since a NaN silently breaks any non-Python journal reader.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class Technicals:
    symbol: str
    last_close: float
    ema_fast: float
    ema_slow: float
    trend: str                # "up" | "down" | "flat" - EMA structure
    rsi: float
    atr: float                # absolute price units
    atr_pct: float            # ATR / close
    range_high: float         # 100-bar high
    range_low: float          # 100-bar low
    range_position: float     # 0 = at lows, 1 = at highs
    ret_20: float             # 20-bar return
    ret_100: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_prompt_block(self) -> str:
        return (
            f"Last close: {self.last_close:.5f}\n"
            f"Trend (EMA20 vs EMA50): {self.trend} "
            f"(fast {self.ema_fast:.5f} / slow {self.ema_slow:.5f})\n"
            f"RSI(14): {self.rsi:.1f}\n"
            f"ATR(14): {self.atr:.5f} ({self.atr_pct:.2%} of price)\n"
            f"100-bar range: {self.range_low:.5f} - {self.range_high:.5f}, "
            f"price sits at {self.range_position:.0%} of that range\n"
            f"Return: {self.ret_20:+.2%} over 20 bars, {self.ret_100:+.2%} over 100 bars"
        )


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    last_gain = float(gain.iloc[-1])
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        # No losses anywhere in the window - RS is a division by zero, not
        # a real value, but the two ways to get here are not the same
        # reading (Phase N7 audit finding M2): a monotonically *rising*
        # tape (last_gain > 0) is Wilder's actual maximally-overbought
        # case - RSI is 100 as rs -> infinity, the mirror image of the
        # already-correct pure-downtrend path below, which returns 0.0.
        # Only a truly flat tape (last_gain == 0 too - delta is 0
        # throughout) has no direction to report, and is genuinely 50.0.
        # Both are real Wilder-RSI values, not the NaN placeholder this
        # replaced (owed from the Phase N2 audit's finding F2: a bare NaN
        # isn't valid JSON, RFC 8259, and broke any non-Python journal
        # reader) - fixing that must not trade one silent wrong value for
        # another.
        return 50.0 if last_gain == 0 else 100.0
    rs = last_gain / last_loss
    return 100 - 100 / (1 + rs)


def _atr(bars: pd.DataFrame, period: int = 14) -> float:
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev = close.shift()
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])


def compute(symbol: str, bars: pd.DataFrame) -> Technicals:
    if len(bars) < 100:
        raise ValueError(f"need >= 100 bars for technicals, got {len(bars)}")

    close = bars["close"].astype(float)
    ema_fast = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema_slow = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    last = float(close.iloc[-1])

    spread = (ema_fast - ema_slow) / ema_slow if ema_slow else 0.0
    trend = "up" if spread > 0.0005 else "down" if spread < -0.0005 else "flat"

    window = close.tail(100)
    hi, lo = float(window.max()), float(window.min())
    atr = _atr(bars)

    return Technicals(
        symbol=symbol,
        last_close=last,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        trend=trend,
        rsi=_rsi(close),
        atr=atr,
        atr_pct=atr / last if last else 0.0,
        range_high=hi,
        range_low=lo,
        range_position=(last - lo) / (hi - lo) if hi > lo else 0.5,
        ret_20=float(close.iloc[-1] / close.iloc[-21] - 1),
        ret_100=float(close.iloc[-1] / close.iloc[-101] - 1),
    )
