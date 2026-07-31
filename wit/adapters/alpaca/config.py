"""Configuration for the Alpaca execution adapter (``providers.py``/``execution.py``).

Mirrors the shape ``nautilus_trader.adapters.interactive_brokers.config`` uses:
an ``InstrumentProviderConfig`` subclass plus a ``LiveExecClientConfig`` subclass,
both carrying the credentials/``paper`` flag the concrete client needs to build
an ``alpaca.trading.client.TradingClient``. There is no data-client config here —
Alpaca supplies execution only (build plan's role split); bar data comes from
``wit/adapters/polygon/config.py``'s ``PolygonDataClientConfig``.
"""
from __future__ import annotations

from nautilus_trader.config import InstrumentProviderConfig, LiveExecClientConfig


class AlpacaInstrumentProviderConfig(InstrumentProviderConfig, frozen=True):
    """Configuration for ``AlpacaInstrumentProvider``.

    Parameters
    ----------
    api_key : str
        The Alpaca API key ID.
    secret_key : str
        The Alpaca API secret key.
    paper : bool, default True
        If True, resolves instruments against Alpaca's paper trading API. Never
        set False outside a deliberate, reviewed go-live (build plan §1.4 - the
        paper-only guarantee this swap must preserve, see ``node_live.py``'s
        ``assert_paper_only``).

    """

    api_key: str = ""
    secret_key: str = ""
    paper: bool = True


class AlpacaExecClientConfig(LiveExecClientConfig, frozen=True):
    """Configuration for ``AlpacaExecutionClient``.

    Parameters
    ----------
    api_key : str
        The Alpaca API key ID.
    secret_key : str
        The Alpaca API secret key.
    paper : bool, default True
        If True, connects to Alpaca's paper trading API
        (``paper-api.alpaca.markets``) for both REST and the trade-update
        WebSocket stream. Never set False outside a deliberate, reviewed
        go-live.

    """

    instrument_provider: AlpacaInstrumentProviderConfig = AlpacaInstrumentProviderConfig()
    api_key: str = ""
    secret_key: str = ""
    paper: bool = True
