"""GARCH risk desk — volatility forecast and position-size multiplier.

Fits a GARCH(1,1) with Student-t innovations to bar returns, forecasts one bar
ahead, annualizes it, and converts that into a vol-targeting size multiplier:

    size_multiplier = target_annual_vol / forecast_annual_vol    (clamped)

So a storm-vol tape shrinks size and a calm tape expands it, bounded by
``RiskConfig.size_multiplier_floor`` / ``_cap``. If the GARCH fit fails to
converge (short or degenerate samples) the desk falls back to trailing realized
vol rather than raising — the pipeline must always get a sizing number.

Ported verbatim from ``Wit-Hedge-fund/engine/signals/garch.py`` (Phase N2).
Gate: byte-identical ``GarchSignal`` output vs. the MT5 build on the same
input DataFrame.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from wit.config import CONFIG, RiskConfig
from wit.desks.contract import GarchSignal, VolRegime

# Bars per year per timeframe, assuming a ~252-day year and a 24h FX session.
BARS_PER_YEAR: dict[str, float] = {
    "M1": 252 * 24 * 60, "M5": 252 * 24 * 12, "M15": 252 * 24 * 4,
    "M30": 252 * 24 * 2, "H1": 252 * 24, "H4": 252 * 6,
    "D1": 252, "W1": 52,
}

_CALM_PCTL = 0.33
_STORM_PCTL = 0.80


def _annualize(sigma_per_bar: float, timeframe: str) -> float:
    """Raises on an unrecognized ``timeframe`` rather than defaulting to 252 bars/year
    (the MT5 original's behavior). That default was harmless there because every caller
    passed an "H1"/"M15"-style literal; it stops being harmless once NautilusTrader
    ``BarType`` strings (e.g. "1-HOUR-LAST") reach this desk in Phase N5+ — a silent
    wrong annualization skews ``size_multiplier`` by roughly 2x with no error and no
    journal trail (see the Phase N2 audit, finding F1). Callers must pass a key that
    exists in ``BARS_PER_YEAR``, translating a Nautilus bar spec first if needed."""
    if timeframe not in BARS_PER_YEAR:
        raise ValueError(
            f"unrecognized timeframe {timeframe!r} for GARCH annualization; "
            f"expected one of {sorted(BARS_PER_YEAR)}"
        )
    return float(sigma_per_bar * np.sqrt(BARS_PER_YEAR[timeframe]))


def _classify(percentile: float) -> VolRegime:
    if percentile < _CALM_PCTL:
        return "calm"
    if percentile > _STORM_PCTL:
        return "storm"
    return "normal"


def _fit_forecast(returns_pct: pd.Series) -> tuple[float, dict]:
    """Return (next-bar sigma in return units, fit diagnostics).

    ``returns_pct`` is in percent (x100) for the optimizer's numerical health.
    """
    from arch import arch_model

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = arch_model(returns_pct, vol="GARCH", p=1, q=1, dist="t", mean="Constant")
        res = model.fit(disp="off", show_warning=False)
        var_next = res.forecast(horizon=1, reindex=False).variance.iloc[-1, 0]

    sigma = float(np.sqrt(var_next)) / 100.0  # back to return units
    params = res.params.to_dict()
    diagnostics = {
        "omega": round(float(params.get("omega", np.nan)), 8),
        "alpha": round(float(params.get("alpha[1]", np.nan)), 4),
        "beta": round(float(params.get("beta[1]", np.nan)), 4),
        "nu": round(float(params.get("nu", np.nan)), 2),
        "converged": bool(res.convergence_flag == 0),
    }
    return sigma, diagnostics


def compute(
    symbol: str,
    bars: pd.DataFrame,
    timeframe: str = "H1",
    risk: RiskConfig | None = None,
    realized_window: int = 100,
) -> GarchSignal:
    """Run the GARCH desk on an OHLCV frame and emit a ``GarchSignal``."""
    risk = risk or CONFIG.risk
    close = bars["close"].astype(float)
    returns = close.pct_change().dropna()
    if len(returns) < 100:
        raise ValueError(f"need >= 100 returns for GARCH, got {len(returns)}")

    # Trailing realized stdev is needed for the ``realized`` output regardless,
    # and doubles as the fallback sigma if the MLE fit fails — compute it once.
    realized_std = float(returns.tail(realized_window).std())

    try:
        sigma_bar, diagnostics = _fit_forecast(returns * 100.0)
        if not np.isfinite(sigma_bar) or sigma_bar <= 0:
            raise ValueError("non-finite GARCH sigma")
    except Exception as exc:  # noqa: BLE001 - sizing must never hard-fail
        sigma_bar = realized_std
        diagnostics = {"converged": False, "fallback": f"realized vol ({exc})"}

    vol_forecast = _annualize(sigma_bar, timeframe)
    realized = _annualize(realized_std, timeframe)

    # Rank the forecast against the recent realized-vol distribution.
    rolling = returns.rolling(realized_window).std().dropna()
    percentile = float((rolling < sigma_bar).mean()) if len(rolling) else 0.5

    raw_mult = risk.target_annual_vol / vol_forecast if vol_forecast > 0 else 1.0
    size_multiplier = float(np.clip(
        raw_mult, risk.size_multiplier_floor, risk.size_multiplier_cap
    ))

    return GarchSignal(
        symbol=symbol,
        vol_forecast=round(vol_forecast, 5),
        vol_regime=_classify(percentile),
        size_multiplier=round(size_multiplier, 3),
        realized_vol=round(realized, 5),
        vol_percentile=round(percentile, 3),
        bars_used=len(returns),
        detail={
            "timeframe": timeframe,
            "sigma_per_bar": round(sigma_bar, 8),
            "target_annual_vol": risk.target_annual_vol,
            "raw_multiplier": round(float(raw_mult), 3),
            "clamped": not np.isclose(raw_mult, size_multiplier),
            **diagnostics,
        },
    )
