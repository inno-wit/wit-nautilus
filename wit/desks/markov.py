"""Markov regime desk — directional view (Markov 2.0).

Implements the corrected markov-hedge-fund-method: classify each bar into a
discrete regime (Bull / Bear / Sideways) from its trailing window return,
then estimate the transition matrix TWO ways and trade only the honest one.

**Fix 1 — stride sampling.** A rolling-window label at bar t shares window-1
of its window underlying bars with the label at bar t+1, so counting every
consecutive pair (the "overlapping" matrix) inflates stickiness with pure
autocorrelation. This desk always builds that legacy matrix for comparison,
but the tradeable signal is read off a second matrix built by sampling the
label series every ``window`` bars (non-overlapping windows -> independent
regime samples). Both matrices, and their diagonals, are reported in
``detail`` so the gap stays visible instead of only living in a backtest.

**Fix 2 — label self-check.** The label series is checked against a small
set of known-regime fixtures (built in for XAUUSD's 2020 COVID crash and
recovery). Any symbol without fixtures, or a call whose bars don't cover a
fixture's date range, reports ``passed: None`` in ``detail`` —  that means
"not checked", not "passed".

**Fix 3 — mode.** This desk is wired as a FILTER: its output is one input to
the committee prompt, never a standalone sized position. ``detail["mode"]``
states this explicitly so it is never ambiguous downstream.

**Threshold scaling.** The original method uses a fixed +/-5% over 20 daily
bars, which is calibrated for equities on D1. On EURUSD H1 that threshold
never fires, so by default the band is *volatility-scaled*: ``threshold = k
* sigma_window``, where ``sigma_window`` is the stdev of window returns.
Pass ``threshold_pct`` to force the classic fixed band instead.

Ported verbatim from ``Wit-Hedge-fund/engine/signals/markov.py`` (Markov 2.0
upgrade). Gate: byte-identical ``MarkovSignal`` output vs. the MT5 build on
the same input DataFrame — see ``tests/test_desks_equivalence.py`` and the
regenerated ``tests/fixtures/mt5_desks.json``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from wit.desks.contract import Direction, MarkovSignal, Regime

STATES: tuple[Regime, ...] = ("Bear", "Sideways", "Bull")
_IDX = {s: i for i, s in enumerate(STATES)}

# Fix 2 fixtures: symbol -> [{name, start, end, expect}]. A live desk usually
# only holds a rolling window of recent bars, so most calls simply won't
# cover these ranges — that reports `passed: None` (skipped), not a failure.
# Add fixtures for another symbol only with real, undisputed regime dates for
# that specific instrument; don't reuse another asset's history.
KNOWN_PERIODS: dict[str, list[dict[str, str]]] = {
    "XAUUSD": [
        {
            "name": "2020 COVID panic (gold sold with everything else)",
            "start": "2020-02-24",
            "end": "2020-03-19",
            "expect": "Bear",
        },
        {
            "name": "2020 post-COVID rally to record highs",
            "start": "2020-04-01",
            "end": "2020-08-07",
            "expect": "Bull",
        },
    ],
}


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
    the sample, which is common on quiet FX pairs. Works on any label series —
    pass the full overlapping series or a stride-sampled one (see
    ``stride_sample``); the counting logic is identical either way.
    """
    counts = np.full((3, 3), float(smoothing))
    prev = regimes.to_numpy()[:-1]
    nxt = regimes.to_numpy()[1:]
    for a, b in zip(prev, nxt):
        counts[_IDX[a], _IDX[b]] += 1.0
    return counts / counts.sum(axis=1, keepdims=True)


def stride_sample(regimes: pd.Series, window: int) -> pd.Series:
    """Fix 1: every ``window``-th label.

    Consecutive labels in the raw series share ``window - 1`` of their
    underlying bars, so counting every consecutive pair (the "overlapping"
    matrix) inflates stickiness with autocorrelation, not persistence.
    Sampling every ``window``-th label instead means each sampled state's
    underlying window doesn't overlap the next one's — consecutive samples
    are independent draws of regime persistence.
    """
    return regimes.iloc[::window]


def stickiness(matrix: np.ndarray) -> dict[str, float]:
    """Diagonal of a transition matrix: P(stay in the same regime)."""
    return {STATES[i]: round(float(matrix[i, i]), 4) for i in range(3)}


def verify_labels(regimes: pd.Series, symbol: str) -> list[dict[str, object]]:
    """Fix 2: majority-label check against known regime fixtures.

    Returns one entry per fixture defined for ``symbol`` (empty list if none
    are defined). ``passed: None`` means the fixture's date range isn't
    covered by the bars supplied — the check was skipped, not passed.
    """
    fixtures = KNOWN_PERIODS.get(symbol, [])
    if not fixtures:
        return []

    idx = regimes.index
    naive_idx = idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx
    naive = pd.Series(regimes.to_numpy(), index=naive_idx)

    results: list[dict[str, object]] = []
    for fx in fixtures:
        start, end = pd.Timestamp(fx["start"]), pd.Timestamp(fx["end"])
        span = naive.loc[(naive.index >= start) & (naive.index <= end)]
        if span.empty:
            results.append({
                "name": fx["name"],
                "range": f"{fx['start']}..{fx['end']}",
                "expected": fx["expect"],
                "observed_majority": None,
                "passed": None,
                "note": "range not covered by the bars supplied",
            })
            continue
        majority = span.value_counts().idxmax()
        results.append({
            "name": fx["name"],
            "range": f"{fx['start']}..{fx['end']}",
            "expected": fx["expect"],
            "observed_majority": majority,
            "passed": bool(majority == fx["expect"]),
        })
    return results


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


def _matrix_dict(matrix: np.ndarray) -> dict[str, dict[str, float]]:
    return {
        STATES[i]: {STATES[j]: round(float(matrix[i, j]), 3) for j in range(3)}
        for i in range(3)
    }


def compute(
    symbol: str,
    bars: pd.DataFrame,
    window: int = 20,
    threshold_pct: float | None = None,
    threshold_k: float = 0.75,
    horizon: int = 1,
) -> MarkovSignal:
    """Run the Markov desk on an OHLCV frame and emit a ``MarkovSignal``.

    The tradeable signal is read off the stride-sampled ("true") matrix, not
    the overlapping ("legacy") one — see module docstring, Fix 1. Both
    matrices and their stickiness are reported in ``detail`` for comparison,
    alongside the Fix 2 label self-check and the Fix 3 mode declaration.
    """
    close = bars["close"].astype(float)
    regimes = classify_regimes(close, window, threshold_pct, threshold_k)

    matrix_overlap = transition_matrix(regimes)
    strided = stride_sample(regimes, window)
    # Fewer than 3 strided samples can't estimate a usable matrix; fall back
    # to the overlapping one rather than trading off noise.
    matrix_stride = transition_matrix(strided) if len(strided) >= 3 else matrix_overlap

    current: Regime = regimes.iloc[-1]
    dist = _forecast(matrix_stride, current, horizon)
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
            # Fix 3 — always gates the committee prompt; never a standalone
            # sized position.
            "mode": "filter",
            "window": window,
            "horizon": horizon,
            "threshold": "fixed" if threshold_pct is not None else f"{threshold_k}-sigma",
            "regime_occupancy": {k: round(v, 3) for k, v in occupancy.items()},
            # Kept for back-compat with existing consumers: this now points at
            # the stride-sampled matrix, since that's what the signal is read
            # from (see the two explicit keys below for the full comparison).
            "transition_matrix": _matrix_dict(matrix_stride),
            "matrix_stride_sampled_true": _matrix_dict(matrix_stride),
            "matrix_overlapping_legacy": _matrix_dict(matrix_overlap),
            "stickiness_stride_sampled_true": stickiness(matrix_stride),
            "stickiness_overlapping_legacy": stickiness(matrix_overlap),
            "n_transitions_stride": max(len(strided) - 1, 0),
            "n_transitions_overlapping": max(len(regimes) - 1, 0),
            "label_verification": verify_labels(regimes, symbol),
        },
    )
