"""Standardized signal contract shared by the quant desks.

Every desk emits a small, JSON-serializable dataclass so the committee prompt
and the risk engine consume one stable shape regardless of the model behind it.

Ported verbatim from ``Wit-Hedge-fund/engine/signals/contract.py`` (Phase N2).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Direction = Literal["BULL", "BEAR", "NEUTRAL"]
Regime = Literal["Bull", "Bear", "Sideways"]
VolRegime = Literal["calm", "normal", "storm"]


@dataclass(frozen=True)
class MarkovSignal:
    """Direction view from the Markov regime desk."""

    symbol: str
    regime: Regime               # current classified regime
    direction: Direction         # sign of the forward-looking edge
    signal: float                # bull_prob - bear_prob, in [-1, 1]
    confidence: float            # [0, 1] - how peaked the forecast is
    bull_prob: float
    bear_prob: float
    sideways_prob: float
    bars_used: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GarchSignal:
    """Volatility / sizing view from the GARCH risk desk."""

    symbol: str
    vol_forecast: float          # 1-bar-ahead sigma, annualized fraction
    vol_regime: VolRegime
    size_multiplier: float       # clamped to [floor, cap] from RiskConfig
    realized_vol: float          # trailing annualized realized vol
    vol_percentile: float        # [0, 1] rank of forecast vs recent history
    bars_used: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
