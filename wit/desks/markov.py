"""Markov regime desk — directional view.

Implements the markov-hedge-fund-method: classify each bar into a discrete
regime (Bull / Bear / Sideways) from its trailing window return, estimate the
empirical transition matrix, then forecast the next-state distribution from the
regime we are currently in. The tradeable output is ``bull_prob - bear_prob``.

**Threshold scaling.** The original method uses a fixed +/-5% over 20 daily bars,
which is calibrated for equities on D1. On EURUSD H1 that threshold never fires,
so by default the band is *volatility-scaled*: ``threshold = k * sigma_window``,
where ``sigma_window`` is the stdev of window returns. Pass ``threshold_pct`` to
force the classic fixed band instead.

Ported verbatim from ``Wit-Hedge-fund/engine/signals/markov.py`` (Phase N2).
Gate: byte-identical ``MarkovSignal`` output vs. the MT5 build on the same
input DataFrame.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from wit.desks.contract import Direction, MarkovSignal, Regime

STATES: tuple[Regime, ...] = ("Bear", "Sideways", "Bull")
_IDX = {s: i for i, s in enumerate(STATES)}


def classify_regimes(
    close: pd.Series,
    window: int = 20,
    threshold_pct: float | None = None,
    threshold_k: float = 0.75,
) -> pd.Series:
    """Label each bar with the regime implied by its trailing ``window`` return.

    Returns a Series of regime labels aligned to ``close`` (first ``window``
    entries dropped, since they have no complete lookback).
    """
    if len(close) <= window + 1:
        raise ValueError(f"need > {window + 1} bars to classify, got {len(close)}")

    win_ret = close.pct_change(window).dropna()

    if threshold_pct is not None:
        thr = pd.Series(float(threshold_pct), index=win_ret.index)
    else:
        # Volatility-scaled band, expanding so it never peeks at the future.
        sigma = win_ret.expanding(min_periods=max(window, 30)).std()
        sigma = sigma.bfill().fillna(win_ret.std() or 1e-9)
        thr = (threshold_k * sigma).clip(lower=1e-9)

    labels = np.where(win_ret > thr, "Bull",
                      np.where(win_ret < -thr, "Bear", "Sideways"))
    return pd.Series(labels, index=win_ret.index, name="regime")


def transition_matrix(regimes: pd.Series, smoothing: float = 1.0) -> np.ndarray:
    """Empirical 3x3 transition matrix with Laplace smoothing.

    Smoothing keeps the matrix well-defined when a regime is rare or absent in
    the sample, which is common on quiet FX pairs.
    """
    counts = np.full((3, 3), float(smoothing))
    prev = regimes.to_numpy()[:-1]
    nxt = regimes.to_numpy()[1:]
    for a, b in zip(prev, nxt):
        counts[_IDX[a], _IDX[b]] += 1.0
    return counts / counts.sum(axis=1, keepdims=True)


def _forecast(matrix: np.ndarray, current: Regime, steps: int = 1) -> np.ndarray:
    """Distribution over regimes ``steps`` bars ahead, given the current regime."""
    vec = np.zeros(3)
    vec[_IDX[current]] = 1.0
    return vec @ np.linalg.matrix_power(matrix, max(1, steps))


def _confidence(dist: np.ndarray) -> float:
    """1 - normalized entropy: 0.0 when uniform, 1.0 when fully concentrated."""
    p = np.clip(dist, 1e-12, 1.0)
    entropy = float(-(p * np.log(p)).sum())
    return float(np.clip(1.0 - entropy / np.log(3.0), 0.0, 1.0))


def compute(
    symbol: str,
    bars: pd.DataFrame,
    window: int = 20,
    threshold_pct: float | None = None,
    threshold_k: float = 0.75,
    horizon: int = 1,
) -> MarkovSignal:
    """Run the Markov desk on an OHLCV frame and emit a ``MarkovSignal``."""
    close = bars["close"].astype(float)
    regimes = classify_regimes(close, window, threshold_pct, threshold_k)
    matrix = transition_matrix(regimes)

    current: Regime = regimes.iloc[-1]
    dist = _forecast(matrix, current, horizon)
    bear_p, side_p, bull_p = (float(x) for x in dist)

    signal = bull_p - bear_p
    # Dead-band keeps marginal edges out of the committee prompt as "BULL"/"BEAR".
    direction: Direction = (
        "BULL" if signal > 0.10 else "BEAR" if signal < -0.10 else "NEUTRAL"
    )

    occupancy = regimes.value_counts(normalize=True).to_dict()
    return MarkovSignal(
        symbol=symbol,
        regime=current,
        direction=direction,
        signal=round(signal, 4),
        confidence=round(_confidence(dist), 4),
        bull_prob=round(bull_p, 4),
        bear_prob=round(bear_p, 4),
        sideways_prob=round(side_p, 4),
        bars_used=len(close),
        detail={
            "window": window,
            "horizon": horizon,
            "threshold": "fixed" if threshold_pct is not None else f"{threshold_k}-sigma",
            "regime_occupancy": {k: round(v, 3) for k, v in occupancy.items()},
            "transition_matrix": {
                STATES[i]: {STATES[j]: round(float(matrix[i, j]), 3) for j in range(3)}
                for i in range(3)
            },
        },
    )
