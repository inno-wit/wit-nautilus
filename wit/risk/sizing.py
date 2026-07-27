"""Risk engine — consensus gate, position sizing, and stop placement.

This module is deliberately deterministic and LLM-free. The committee proposes;
the risk engine disposes. Every trade must clear *all* gates:

    1. The PM approved a direction (not HOLD).
    2. The Markov regime is not strongly opposed to that direction.
    3. Spread is inside the cap.
    4. Concurrent-position and per-symbol caps allow another position.
    5. The computed quantity is at or above the broker's minimum.

Size = equity x risk_per_trade x GARCH size_multiplier x PM conviction, converted
to a tradeable quantity through the stop distance and the instrument's
account-currency value per unit, then clamped by the broker's quantity step
and limits.

Ported from ``Wit-Hedge-fund/engine/risk/sizing.py`` (Phase N4). Gate ordering,
thresholds (``MARKOV_VETO_THRESHOLD``), and every blocked-reason string are
unchanged — that's the risk guarantee this port must not silently move. Two
things genuinely change shape, both forced by the MT5-to-Nautilus unit-system
switch (lots/points -> raw quantity/price), not by any change in what a gate
means — see ``wit/risk/instrument_spec.py``'s module docstring for the full
reasoning:

- ``SymbolSpec`` -> ``InstrumentSpec``; the lot/tick-value indirection
  (``ticks = stop_distance / tick_size; loss_per_lot = ticks * tick_value``)
  collapses to ``loss_per_unit = stop_distance * value_per_unit``, since
  Nautilus has no "lot" concept to convert through.
- ``spread_points: int`` -> ``spread: float`` (native price units); the
  points-based absolute cap is dropped, ``max_spread_pct`` is the sole spread
  gate (see ``instrument_spec.py`` for why "points" isn't a coherent
  cross-instrument concept on IBKR).

``TradePlan.lots`` is renamed ``TradePlan.quantity`` throughout, matching
Nautilus's own vocabulary.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from wit.committee.contract import CommitteeDecision
from wit.config import CONFIG, RiskConfig
from wit.desks.contract import GarchSignal, MarkovSignal
from wit.desks.technicals import Technicals
from wit.risk.account import AccountSnapshot
from wit.risk.instrument_spec import InstrumentSpec

# Markov opposes a direction when its signal leans this far the other way.
MARKOV_VETO_THRESHOLD = 0.35


def _group_of(symbol: str, groups: dict[str, tuple[str, ...]]) -> str | None:
    """The correlation-group name a symbol belongs to, or None."""
    for name, members in groups.items():
        if symbol in members:
            return name
    return None


@dataclass(frozen=True)
class TradePlan:
    """The risk engine's verdict: either an executable order or a blocked reason."""

    symbol: str
    approved: bool
    action: str                      # BUY | SELL | HOLD
    quantity: float = 0.0
    entry: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    risk_amount: float = 0.0         # account currency at risk if stopped
    risk_pct: float = 0.0            # as a fraction of equity
    blocked_by: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _markov_opposes(action: str, mk: MarkovSignal) -> bool:
    """True when the regime desk leans meaningfully against the PM's direction."""
    if action == "BUY":
        return mk.signal < -MARKOV_VETO_THRESHOLD
    if action == "SELL":
        return mk.signal > MARKOV_VETO_THRESHOLD
    return False


def _quantity_for_risk(
    risk_amount: float, stop_distance: float, spec: InstrumentSpec
) -> tuple[float, float]:
    """Convert currency-at-risk into a quantity. Returns (raw_qty, loss_per_unit)."""
    if stop_distance <= 0 or spec.value_per_unit <= 0:
        return 0.0, 0.0
    loss_per_unit = stop_distance * spec.value_per_unit
    if loss_per_unit <= 0:
        return 0.0, 0.0
    return risk_amount / loss_per_unit, loss_per_unit


def build_plan(
    decision: CommitteeDecision,
    tech: Technicals,
    mk: MarkovSignal,
    gk: GarchSignal,
    account: AccountSnapshot,
    spec: InstrumentSpec,
    spread: float,
    open_positions_total: int,
    open_positions_symbol: int,
    risk: RiskConfig | None = None,
    open_symbols: tuple[str, ...] = (),
    in_cooldown: bool = False,
    margin_fn: Callable[[str, str, float], float] | None = None,
    kelly_mult: float = 1.0,
    drawdown_mult: float = 1.0,
) -> TradePlan:
    """Apply the consensus gate and size the position.

    ``spread`` is the live bid/ask spread in native price units (not MT5
    "points" — see module docstring). ``open_symbols`` is the symbols of
    currently-open positions (for the correlation-group cap); ``in_cooldown``
    is set by the caller when this symbol traded too recently;
    ``margin_fn(symbol, side, quantity)`` returns the broker margin needed,
    checked against free margin once the quantity is known. ``kelly_mult`` /
    ``drawdown_mult`` are the adaptive-sizing multipliers (see
    ``wit/risk/adaptive.py``); both default to 1.0 (no effect).
    """
    risk = risk or CONFIG.risk
    symbol = decision.symbol
    action = decision.action
    blocked: list[str] = []

    # ── Gate 1-4: reasons not to trade at all ────────────────────────────
    if action == "HOLD":
        blocked.append("PM decision is HOLD")
    if action != "HOLD" and decision.conviction < risk.min_conviction:
        blocked.append(
            f"conviction {decision.conviction:.2f} below floor {risk.min_conviction:.2f}"
        )
    if in_cooldown:
        blocked.append(
            f"{symbol} in post-exit cooldown ({risk.cooldown_minutes}m re-entry throttle)"
        )
    group = _group_of(symbol, risk.correlation_groups)
    if group is not None:
        peers = sum(
            1 for s in open_symbols
            if s != symbol and s in risk.correlation_groups[group]
        )
        if peers >= risk.max_positions_per_group:
            blocked.append(
                f"correlated group '{group}' already holds {peers} position(s) "
                f"(cap {risk.max_positions_per_group})"
            )
    if _markov_opposes(action, mk):
        blocked.append(
            f"Markov regime opposes {action} (signal {mk.signal:+.2f}, regime {mk.regime})"
        )
    # A malformed quote (broken feed, de-listed contract, crossed/inverted book)
    # must block, not silently disable the spread gate. MT5's spread_points was a
    # broker-reported non-negative int, so this couldn't arise there; spread is
    # now a caller-supplied float (ask - bid), which can be garbage. Phase N4
    # audit findings F1/F2.
    if tech.last_close <= 0:
        blocked.append("no valid last price (last_close <= 0) - quote appears broken")
    elif spread < 0:
        blocked.append(f"spread {spread} is negative - quote appears crossed/broken")
    spread_pct = (spread / tech.last_close) if tech.last_close > 0 else 0.0
    if spread_pct > risk.max_spread_pct:
        blocked.append(
            f"spread {spread_pct:.3%} of price exceeds cap {risk.max_spread_pct:.3%}"
        )
    if open_positions_total >= risk.max_concurrent_positions:
        blocked.append(
            f"already at max concurrent positions ({risk.max_concurrent_positions})"
        )
    if open_positions_symbol >= risk.per_symbol_max_positions:
        blocked.append(
            f"already holding {open_positions_symbol} position(s) in {symbol}"
        )

    # ── Sizing ───────────────────────────────────────────────────────────
    # Adaptive multipliers (both 1.0 unless configured/triggered): fractional
    # Kelly scales by recent edge, the drawdown throttle shrinks size in a
    # losing day. They ride on top of the base risk, never around the gates.
    size_mult = kelly_mult * drawdown_mult
    risk_amount = (
        account.equity * risk.risk_per_trade * gk.size_multiplier
        * decision.conviction * size_mult
    )
    stop_distance = decision.stop_atr_mult * tech.atr

    # Respect the configured minimum stop distance (build plan §1.3: no IB
    # equivalent to MT5's broker-reported stops_level_points).
    if spec.min_stop_distance > 0 and stop_distance < spec.min_stop_distance:
        stop_distance = spec.min_stop_distance

    raw_qty, loss_per_unit = _quantity_for_risk(risk_amount, stop_distance, spec)
    quantity = spec.round_quantity(raw_qty) if raw_qty > 0 else 0.0

    if raw_qty > 0 and raw_qty < spec.min_quantity:
        blocked.append(
            f"risk budget sizes to {raw_qty:.4f} units, below broker minimum "
            f"{spec.min_quantity}"
        )
    if raw_qty <= 0 and action != "HOLD":
        blocked.append("sizing produced zero quantity (check ATR / instrument spec)")

    entry = tech.last_close
    if action == "BUY":
        sl = entry - stop_distance
        tp = entry + stop_distance * decision.reward_risk
    elif action == "SELL":
        sl = entry + stop_distance
        tp = entry - stop_distance * decision.reward_risk
    else:
        sl = tp = 0.0

    # ── Margin gate: needs the rounded quantity, so it runs after sizing ──
    if margin_fn is not None and quantity > 0 and action != "HOLD":
        try:
            needed = margin_fn(symbol, action, quantity)
        except Exception:  # noqa: BLE001 - a margin-calc failure must not crash the cycle
            needed = 0.0
        if needed > 0 and needed > account.margin_free:
            blocked.append(
                f"margin {needed:.2f} exceeds free margin {account.margin_free:.2f}"
            )

    approved = not blocked
    if not approved:
        quantity = 0.0
    # Actual risk reflects the *rounded* quantity, not the ideal one.
    actual_risk = quantity * loss_per_unit

    return TradePlan(
        symbol=symbol,
        approved=approved,
        action=action if approved else "HOLD",
        quantity=quantity,
        entry=spec.round_price(entry),
        stop_loss=spec.round_price(sl),
        take_profit=spec.round_price(tp),
        risk_amount=round(actual_risk, 2),
        risk_pct=round(actual_risk / account.equity, 5) if account.equity else 0.0,
        blocked_by=blocked,
        detail={
            "conviction": decision.conviction,
            "garch_size_multiplier": gk.size_multiplier,
            "markov_signal": mk.signal,
            "base_risk_pct": risk.risk_per_trade,
            "kelly_mult": round(kelly_mult, 3),
            "drawdown_mult": round(drawdown_mult, 3),
            "target_risk_amount": round(risk_amount, 2),
            "stop_distance": spec.round_price(stop_distance),
            "stop_atr_mult": decision.stop_atr_mult,
            "atr": spec.round_price(tech.atr),
            "reward_risk": decision.reward_risk,
            "raw_quantity": round(raw_qty, 4),
            "loss_per_unit": round(loss_per_unit, 2),
            "spread": spread,
            "spread_pct": round(spread_pct, 5),
        },
    )


def revalidate_plan(
    plan: TradePlan,
    bid: float,
    ask: float,
    spec: InstrumentSpec,
    spread: float,
    risk: RiskConfig | None = None,
) -> str | None:
    """Re-check an approved plan against the live market right before execution.

    ``build_plan`` derives absolute SL/TP levels from the *analysed bar's* close,
    but the order is only sent after the committee's LLM calls (up to a few
    minutes later). This guards against price having drifted: it returns a
    rejection reason (block the order), or ``None`` when the plan is still sane.
    """
    risk = risk or CONFIG.risk
    if not plan.approved or plan.action == "HOLD":
        return "plan is not an executable order"

    live_entry = ask if plan.action == "BUY" else bid
    if live_entry <= 0:
        return "no live price available"
    if spread < 0:
        return f"live spread {spread} is negative - quote appears crossed/broken"

    if plan.entry > 0:
        drift = abs(live_entry - plan.entry) / plan.entry
        if drift > risk.max_entry_slippage_pct:
            return (f"price drifted {drift:.3%} from planned entry "
                    f"(cap {risk.max_entry_slippage_pct:.3%})")

    spread_pct = (spread / live_entry) if live_entry > 0 else 0.0
    if spread_pct > risk.max_spread_pct:
        return f"live spread {spread_pct:.3%} of price exceeds cap {risk.max_spread_pct:.3%}"

    # SL/TP must still bracket the live price on the correct side.
    if plan.action == "BUY":
        if not (plan.stop_loss < live_entry < plan.take_profit):
            return "SL/TP no longer bracket live price for a BUY"
    else:  # SELL
        if not (plan.take_profit < live_entry < plan.stop_loss):
            return "SL/TP no longer bracket live price for a SELL"

    # Respect the configured minimum stop distance from the *live* price.
    if spec.min_stop_distance > 0 and (
        abs(live_entry - plan.stop_loss) < spec.min_stop_distance
        or abs(plan.take_profit - live_entry) < spec.min_stop_distance
    ):
        return "SL/TP inside broker minimum stop distance at live price"

    return None
