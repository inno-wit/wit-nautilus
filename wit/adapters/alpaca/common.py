"""Shared constants for the Alpaca adapter.

**Single venue, execution-only** (build plan §"Architecture", the broker swap's
load-bearing design decision, verified against the installed
``nautilus_trader==1.230.0`` in Phase 0 of the swap): every ``InstrumentId`` this
system trades uses ``ALPACA_VENUE``, including the ones whose *bars* come from
Polygon, not Alpaca. ``AlpacaInstrumentProvider`` defines the real tradable
instruments (used for order placement); ``PolygonDataClient`` publishes
``Bar``/``QuoteTick`` objects stamped with those same ``InstrumentId``s via
NautilusTrader's ``RoutingConfig.venues``/``DataEngine.register_venue_routing``
mechanism (confirmed in ``data/engine.pyx``'s ``register_venue_routing`` — a data
client's own ``.venue`` need not match the venue it's routed to serve). A single
Alpaca account trading every watchlist symbol still has one equity figure, so
``FundStateActor`` is configured with this same venue.
"""
from __future__ import annotations

from nautilus_trader.model.identifiers import Venue

ALPACA_VENUE = Venue("ALPACA")
