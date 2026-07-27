"""Quant Analyst node — packages the deterministic desks into one committee input.

Technicals, Markov (direction) and GARCH (vol/sizing) are each computed
independently, plus optional market intelligence. This node's only job is to
package them into a single, stable object the committee reads from and journal
writes, and to add one cheap deterministic synthesis (``agreement``) stating
outright whether the technical trend and the Markov regime agree.

No LLM calls happen here and none of the underlying desks are recomputed.

Ported verbatim from ``Wit-Hedge-fund/engine/signals/quant_analyst.py`` (Phase N2).
Gate: byte-identical ``as_prompt_block()`` output vs. the MT5 build.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from wit.desks.contract import GarchSignal, MarkovSignal
from wit.desks.market_intel import MarketIntel
from wit.desks.technicals import Technicals
from wit.ops.dream import DreamState

_QUANT_BLOCK = """\
== {symbol} · {timeframe} ==

TECHNICAL DESK
{technicals}

MARKOV REGIME DESK (direction)
Current regime: {regime}
Forecast next bar: bull {bull_prob:.1%} / sideways {sideways_prob:.1%} / bear {bear_prob:.1%}
Directional signal (bull-bear): {signal:+.3f}   Confidence: {confidence:.2f}
Regime occupancy in sample: {occupancy}

GARCH RISK DESK (volatility)
1-bar-ahead annualized vol forecast: {vol_forecast:.2%}
Trailing realized vol: {realized_vol:.2%}
Volatility regime: {vol_regime} (forecast sits at the {vol_percentile:.0%} percentile)
Vol-target size multiplier: {size_multiplier:.2f}x

QUANT ANALYST READ
Technicals vs. regime: {agreement}
"""

_INTEL_BLOCK = """
MARKET INTELLIGENCE DESK (fundamentals + news, real data via yfinance{finnhub_note})
{body}
"""


def _agreement(tech: Technicals, mk: MarkovSignal) -> str:
    """Deterministic read of whether the technical trend and the Markov
    regime point the same way."""
    tech_dir = {"up": "BULL", "down": "BEAR"}.get(tech.trend, "NEUTRAL")
    if tech_dir == "NEUTRAL" or mk.direction == "NEUTRAL":
        return "no clear alignment"
    if tech_dir == mk.direction:
        return f"aligned {mk.direction.lower()}"
    return "conflicted (technicals vs regime)"


@dataclass(frozen=True)
class QuantAnalystReport:
    """The committee's single quant input, and what the journal logs under
    ``"quant"``."""

    symbol: str
    timeframe: str
    technicals: Technicals
    markov: MarkovSignal
    garch: GarchSignal
    intel: MarketIntel | None = None
    agreement: str = "no clear alignment"
    dream: DreamState | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "markov": self.markov.to_dict(),
            "garch": self.garch.to_dict(),
            "technicals": self.technicals.to_dict(),
            "intel": self.intel.to_dict() if self.intel is not None else None,
            "agreement": self.agreement,
            "dream": self.dream.to_dict() if self.dream is not None else None,
        }

    def as_prompt_block(self) -> str:
        tech, mk, gk = self.technicals, self.markov, self.garch
        block = _QUANT_BLOCK.format(
            symbol=self.symbol, timeframe=self.timeframe,
            technicals=tech.as_prompt_block(),
            regime=mk.regime, bull_prob=mk.bull_prob, bear_prob=mk.bear_prob,
            sideways_prob=mk.sideways_prob, signal=mk.signal, confidence=mk.confidence,
            occupancy=json.dumps(mk.detail.get("regime_occupancy", {})),
            vol_forecast=gk.vol_forecast, realized_vol=gk.realized_vol,
            vol_regime=gk.vol_regime, vol_percentile=gk.vol_percentile,
            size_multiplier=gk.size_multiplier,
            agreement=self.agreement,
        )
        if self.intel is not None and self.intel.has_content:
            finnhub_note = "" if not self.intel.analyst_summary else " + Finnhub"
            block += _INTEL_BLOCK.format(finnhub_note=finnhub_note,
                                         body=self.intel.as_prompt_block())
        if self.dream is not None:
            dream_block = self.dream.as_prompt_block()
            if dream_block:
                block += "\n" + dream_block + "\n"
        return block


def compute(
    symbol: str, timeframe: str, tech: Technicals, mk: MarkovSignal,
    gk: GarchSignal, intel: MarketIntel | None = None, dream: DreamState | None = None,
) -> QuantAnalystReport:
    """Package the already-computed desks into a ``QuantAnalystReport``."""
    return QuantAnalystReport(
        symbol=symbol, timeframe=timeframe, technicals=tech, markov=mk,
        garch=gk, intel=intel, agreement=_agreement(tech, mk), dream=dream,
    )
