"""Configuration for the Polygon data adapter (``data.py``).

``max_requests_per_minute`` defaults to 5 — Phase 0 of the broker swap confirmed
this account is on Polygon/Massive's free tier (a real-time endpoint call
returned ``403 NOT_AUTHORIZED``, and Phase 1's watchlist check independently hit
the 5/min cap live). This is not a theoretical ceiling to design around later;
it is the account's actual, verified limit, and ``PolygonDataClient`` is built
REST-polled from the start rather than assuming WebSocket streaming.
"""
from __future__ import annotations

from nautilus_trader.config import LiveDataClientConfig


class PolygonDataClientConfig(LiveDataClientConfig, frozen=True):
    """Configuration for ``PolygonDataClient``.

    Parameters
    ----------
    api_key : str
        The Polygon/Massive API key.
    max_requests_per_minute : int, default 5
        The account's confirmed rate limit, enforced client-side across every
        REST call this client makes (historical bar requests, live bar polling,
        live quote polling all share one limiter) so the free tier's 429/error
        response is a design input, not a runtime surprise.
    poll_interval_secs : float, default 20.0
        How often each subscribed bar/quote polling loop checks Polygon for new
        data. Independent of the actual bar timeframe (``CONFIG.timeframe``,
        e.g. "H1") - this only controls how promptly a newly closed bar is
        noticed, not how often bars close. Data is delayed ~15 minutes on the
        free tier regardless of poll frequency.
    delayed_minutes : int, default 15
        The account's confirmed data delay, used only to log an honest staleness
        note - not enforced, since Polygon's response timestamps are themselves
        already delayed.
    alpaca_api_key : str
        Alpaca's API key ID, so this client can share the same cached
        ``AlpacaInstrumentProvider``/``TradingClient`` instance
        ``AlpacaLiveExecClientFactory`` builds (see ``wit/adapters/alpaca/
        factories.py``) — Alpaca defines the tradable instruments; Polygon
        only supplies bars for them. NautilusTrader's ``LiveDataClientFactory.
        create`` signature is fixed (``loop, name, config, msgbus, cache,
        clock`` only), so this can't be passed as a separate factory argument
        and must travel on the config instead.
    alpaca_secret_key : str
        Alpaca's API secret key, for the same reason.
    alpaca_paper : bool, default True
        Must match ``AlpacaExecClientConfig.paper`` — both adapters share one
        cache key of ``(alpaca_api_key, alpaca_paper)``.

    """

    api_key: str = ""
    max_requests_per_minute: int = 5
    poll_interval_secs: float = 20.0
    delayed_minutes: int = 15
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
