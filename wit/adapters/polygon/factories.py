"""``PolygonLiveDataClientFactory`` — wires a Nautilus ``TradingNodeConfig``'s
``data_clients={"POLYGON": ...}`` entry to a concrete ``PolygonDataClient``,
sharing the same ``AlpacaInstrumentProvider`` instance
``AlpacaLiveExecClientFactory`` caches (see that module's docstring) so both
clients agree on exactly the same set of ``Instrument`` objects.
"""
from __future__ import annotations

import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.live.factories import LiveDataClientFactory

from wit.adapters.alpaca.factories import (
    get_cached_alpaca_instrument_provider,
    get_cached_alpaca_trading_client,
)
from wit.adapters.polygon.config import PolygonDataClientConfig
from wit.adapters.polygon.data import PolygonDataClient


class PolygonLiveDataClientFactory(LiveDataClientFactory):
    """Provides a Polygon live data client factory."""

    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: PolygonDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> PolygonDataClient:
        # NautilusTrader's node_builder calls `factory.create(loop, name, config,
        # msgbus, cache, clock)` with no room for extra arguments (confirmed
        # against the installed live/node_builder.py), so Alpaca's credentials
        # travel on `config` itself (see PolygonDataClientConfig's docstring)
        # rather than as separate factory parameters.
        #
        # The instrument provider is Alpaca-backed (Alpaca defines the tradable
        # instruments; Polygon only supplies bars for them - see
        # wit/adapters/alpaca/common.py's module docstring), cached under the
        # same (api_key, paper) key AlpacaLiveExecClientFactory uses so both
        # clients share one instance and one `initialize()` call's result.
        trading_client = get_cached_alpaca_trading_client(
            config.alpaca_api_key, config.alpaca_secret_key, config.alpaca_paper,
        )
        # `config.instrument_provider` here is a plain InstrumentProviderConfig
        # (inherited from LiveDataClientConfig) - on a cache miss this seeds the
        # shared provider with whatever `load_ids` node_live.py set on Polygon's
        # own config; in the normal boot order (Alpaca's exec client builds
        # first) the provider is already cached and this argument is unused.
        instrument_provider = get_cached_alpaca_instrument_provider(
            trading_client, clock, config.instrument_provider,
            config.alpaca_api_key, config.alpaca_paper,
        )
        return PolygonDataClient(
            loop=loop,
            name=name,
            instrument_provider=instrument_provider,
            config=config,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
