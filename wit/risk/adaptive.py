"""Adaptive position sizing — fractional Kelly + drawdown throttle.

Two deterministic multipliers layered on top of the base risk budget
(``equity × risk_per_trade × GARCH size × conviction``). Both default to 1.0
(no effect) so they change nothing until they actually apply — the risk engine
stays pure and the existing gates are untouched.

- **Drawdown throttle** (on by default — pure safety): as the *realized* loss on
  the current trading day grows toward the daily-loss cap, size scales down
  toward a floor. It never scales *up*; a green day trades at full size. This is
  a smooth pre-cursor to the hard daily-loss breaker — it leans on the brakes
  before the circuit trips.
- **Fractional Kelly** (opt-in, off by default): scales size by the edge implied
  by recent realized trades — win rate ``p`` and payoff ``b = avg_win/avg_loss``
  give the full-Kelly capital fraction ``f* = p − (1−p)/b``, taken at
  ``kelly_fraction`` (0.25×) and expressed as a multiple of the base risk, then
  clamped. **Sample-gated:** below ``kelly_min_trades`` closed trades it returns
  1.0, because Kelly on a thin sample estimates noise, not edge (the same reason
  the dream cycle enforces a per-bucket floor). When the estimator sees no edge
  (``f* ≤ 0``) the multiplier clamps to its floor rather than zero — reduce, not
  refuse.

Ported verbatim from ``Wit-Hedge-fund/engine/risk/adaptive.py`` (Phase N4) —
pure math, no broker/instrument coupling, so nothing here changes with the
MT5-to-Nautilus port.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import mean

from wit.config import AdaptiveConfig


@dataclass(frozen=True)
class KellyStats:
    wins: int
    losses: int
    avg_win: float          # mean of winning P&L (>= 0)
    avg_loss: float         # mean of losing P&L as a positive magnitude (>= 0)

    @property
    def trades(self) -> int:
        return self.wins + self.losses


def kelly_stats(pnls: Iterable[float]) -> KellyStats:
    """Summarize a set of realized trade P&Ls into the inputs Kelly needs.

    Break-even trades (exactly 0.0) count as neither a win nor a loss."""
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    return KellyStats(
        wins=len(wins),
        losses=len(losses),
        avg_win=mean(wins) if wins else 0.0,
        avg_loss=mean(losses) if losses else 0.0,
    )


def kelly_multiplier(
    stats: KellyStats, base_risk_per_trade: float, cfg: AdaptiveConfig
) -> float:
    """Size multiplier from recent edge, in ``[kelly_mult_floor, kelly_mult_cap]``.

    Returns 1.0 (no effect) when disabled, under-sampled, or the inputs are
    degenerate."""
    if not cfg.use_fractional_kelly or stats.trades < cfg.kelly_min_trades:
        return 1.0
    if stats.avg_loss <= 0 or base_risk_per_trade <= 0:
        return 1.0
    p = stats.wins / stats.trades
    b = stats.avg_win / stats.avg_loss
    if b <= 0:
        return 1.0
    f_star = p - (1.0 - p) / b                  # full-Kelly fraction of capital
    frac = max(0.0, f_star) * cfg.kelly_fraction
    mult = frac / base_risk_per_trade           # as a multiple of the base risk
    return max(cfg.kelly_mult_floor, min(mult, cfg.kelly_mult_cap))


def drawdown_multiplier(
    realized_pnl: float,
    start_equity: float,
    daily_loss_cap: float,
    floor: float,
    enabled: bool = True,
) -> float:
    """Throttle size as today's realized loss approaches the daily-loss cap.

    ``1.0`` at break-even or in profit; scales linearly down to ``floor`` once the
    realized loss reaches ``daily_loss_cap × start_equity`` (where the hard
    breaker takes over). Never exceeds 1.0."""
    if not enabled or realized_pnl >= 0 or start_equity <= 0 or daily_loss_cap <= 0:
        return 1.0
    loss_frac = -realized_pnl / start_equity
    progress = min(loss_frac / daily_loss_cap, 1.0)
    return round(floor + (1.0 - floor) * (1.0 - progress), 4)
