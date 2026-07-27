"""``InstrumentSpec`` — the build plan §1.3 shim that replaces MT5's
``engine.broker.base.SymbolSpec`` for ``wit.risk.sizing``.

New, not a port: MT5's ``SymbolSpec`` (``digits``, ``point``, ``tick_size``,
``tick_value``, ``volume_min/max/step``, ``stops_level_points``) is expressed
in concepts specific to MT5's lot-based, "points" quoting model. Nautilus/IBKR
trade in raw instrument quantity units (shares, base-currency units) directly
— there is no "lot" indirection to convert through — so this is a genuine
redesign of the shape, not a renamed copy. The two things this loses on
purpose:

- **"Points" as a cross-instrument concept.** A "point" is broker/instrument
  specific on MT5; IBKR has no equivalent. ``wit.risk.sizing`` now takes
  spread directly in price units and gates on ``RiskConfig.max_spread_pct``
  only — ``max_spread_points`` is dropped from ``RiskConfig`` entirely (it
  was already the build plan's own diagnosis that a points-only cap
  miscalibrates across FX pips vs $-priced equities; with "points" no longer
  coherent at all, the pct cap is the sole, correct gate).
- **``stops_level_points`` (MT5's broker-reported minimum stop distance).**
  No IB equivalent exists. Replaced by ``min_stop_distance``, a configured
  floor in price units — same "widen the stop, don't reject" behavior, just
  operator-set per instrument instead of broker-reported.

``spec_for`` is duck-typed against anything exposing
``price_increment``/``size_increment``/``min_quantity``/``max_quantity``
(the fields Nautilus's ``Instrument`` confirmed in Phase N0's live probing),
not typed against ``nautilus_trader.model.instruments.Instrument`` directly —
that class is Cython-compiled and needs a running kernel to construct even in
tests (see Phase N0's notes on why raw ``ibapi`` was used for that spike
instead). Keeping this module framework-free means ``wit.risk.sizing``'s
tests don't need ``nautilus_trader`` installed at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InstrumentSpec:
    """Contract specification needed to turn risk-in-currency into a
    tradeable quantity."""

    instrument_id: str
    price_increment: float   # smallest tradeable price step
    min_quantity: float
    quantity_step: float
    max_quantity: float | None = None
    # Account-currency P&L per 1.0 price-unit move, per 1 unit of quantity.
    # 1.0 for every instrument on the current watchlist: US equities (1 share
    # = 1 unit) and EURUSD held in a USD account (quote currency is already
    # USD, so no conversion). Override when a future instrument's quote
    # currency differs from the account currency.
    value_per_unit: float = 1.0
    # Configured floor in price units — see module docstring. 0.0 = no floor.
    min_stop_distance: float = 0.0

    def round_price(self, price: float) -> float:
        """Snap to the instrument's price increment."""
        if self.price_increment <= 0:
            return price
        steps = round(price / self.price_increment)
        return round(steps * self.price_increment, 10)

    def round_quantity(self, qty: float) -> float:
        """Snap to the broker's quantity step and clamp to its limits."""
        if self.quantity_step <= 0:
            snapped = qty
        else:
            steps = round(qty / self.quantity_step)
            snapped = steps * self.quantity_step
        snapped = max(self.min_quantity, snapped)
        if self.max_quantity is not None:
            snapped = min(snapped, self.max_quantity)
        # Quantity steps are decimals like 0.01 (FX) or 1 (equities); round
        # away float noise.
        return round(snapped, 8)


def spec_for(
    instrument: Any,
    *,
    value_per_unit: float = 1.0,
    min_stop_distance: float = 0.0,
) -> InstrumentSpec:
    """Build an ``InstrumentSpec`` from a Nautilus ``Instrument`` (or anything
    duck-typed the same way — see module docstring)."""
    max_quantity = getattr(instrument, "max_quantity", None)
    min_quantity = getattr(instrument, "min_quantity", None)
    size_increment = float(instrument.size_increment)
    return InstrumentSpec(
        instrument_id=str(instrument.id),
        price_increment=float(instrument.price_increment),
        min_quantity=float(min_quantity) if min_quantity is not None else size_increment,
        quantity_step=size_increment,
        max_quantity=float(max_quantity) if max_quantity is not None else None,
        value_per_unit=value_per_unit,
        min_stop_distance=min_stop_distance,
    )
