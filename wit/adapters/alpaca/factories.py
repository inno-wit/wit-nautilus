"""``AlpacaLiveExecClientFactory`` — wires a Nautilus ``TradingNodeConfig``'s
``exec_clients={"ALPACA": ...}`` entry to a concrete ``AlpacaExecutionClient``.

No ``AlpacaLiveDataClientFactory`` — Alpaca supplies execution only in this
build's role split (build plan's "Architecture" section); bar data comes from
``wit/adapters/polygon/factories.py``'s ``PolygonLiveDataClientFactory``.

The ``TradingClient`` and ``AlpacaInstrumentProvider`` are cached per
``(api_key, paper)`` key (mirrors ``interactive_brokers.factories``'s
``get_cached_ib_client`` pattern) so the same provider instance is shared with
``PolygonLiveDataClientFactory`` when both are built into the same node - this
is what lets a single ``AlpacaInstrumentProviderConfig`` in ``node_live.py``'s
``build_config()`` govern both.
"""
from __future__ import annotations

import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.live.factories import LiveExecClientFactory

from wit.adapters.alpaca.config import AlpacaExecClientConfig
from wit.adapters.alpaca.execution import AlpacaExecutionClient
from wit.adapters.alpaca.providers import AlpacaInstrumentProvider

ALPACA_CLIENTS: dict[tuple, object] = {}
ALPACA_STREAMS: dict[tuple, object] = {}
ALPACA_PROVIDERS: dict[tuple, AlpacaInstrumentProvider] = {}


def get_cached_alpaca_trading_client(api_key: str, secret_key: str, paper: bool):
    from alpaca.trading.client import TradingClient

    key = (api_key, paper)
    if key not in ALPACA_CLIENTS:
        ALPACA_CLIENTS[key] = TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)
    return ALPACA_CLIENTS[key]


def get_cached_alpaca_trading_stream(api_key: str, secret_key: str, paper: bool):
    from alpaca.trading.stream import TradingStream

    key = (api_key, paper)
    if key not in ALPACA_STREAMS:
        ALPACA_STREAMS[key] = TradingStream(api_key=api_key, secret_key=secret_key, paper=paper)
    return ALPACA_STREAMS[key]


def get_cached_alpaca_instrument_provider(
    trading_client, clock: LiveClock, instrument_provider_config, api_key: str, paper: bool,
) -> AlpacaInstrumentProvider:
    """``instrument_provider_config`` need only be an ``InstrumentProviderConfig``
    (``load_ids``/``load_all``/``filters``) - both ``AlpacaExecClientConfig.
    instrument_provider`` and ``PolygonDataClientConfig.instrument_provider``
    (inherited from ``LiveDataClientConfig``) satisfy this without any adapting,
    which is what lets both factories share one cached provider per (api_key, paper)."""
    key = (api_key, paper)
    if key not in ALPACA_PROVIDERS:
        ALPACA_PROVIDERS[key] = AlpacaInstrumentProvider(
            trading_client=trading_client, clock=clock, config=instrument_provider_config,
        )
    return ALPACA_PROVIDERS[key]


class AlpacaLiveExecClientFactory(LiveExecClientFactory):
    """Provides an Alpaca live execution client factory."""

    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: AlpacaExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> AlpacaExecutionClient:
        trading_client = get_cached_alpaca_trading_client(
            config.api_key, config.secret_key, config.paper,
        )
        trading_stream = get_cached_alpaca_trading_stream(
            config.api_key, config.secret_key, config.paper,
        )
        instrument_provider = get_cached_alpaca_instrument_provider(
            trading_client, clock, config.instrument_provider, config.api_key, config.paper,
        )
        return AlpacaExecutionClient(
            loop=loop,
            trading_client=trading_client,
            trading_stream=trading_stream,
            instrument_provider=instrument_provider,
            config=config,
            name=name,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
