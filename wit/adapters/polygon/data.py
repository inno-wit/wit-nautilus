"""``PolygonDataClient`` — bar (and synthetic quote) data from Polygon/Massive's
REST API, polled rather than streamed (Phase 0 of the broker swap confirmed this
account is free-tier: no real-time WebSocket entitlement, ~15 min delay).

Publishes under ``ALPACA_VENUE``-tagged ``InstrumentId``s (see
``wit/adapters/alpaca/common.py``'s module docstring for why) using
NautilusTrader's client-routing mechanism — this client's own ``venue`` is
``None`` (multi-venue-capable, per ``LiveDataClient``'s docstring), and
``node_live.py`` registers it with ``routing=RoutingConfig(venues={"ALPACA"})``
so the ``DataEngine`` sends ``ALPACA``-addressed data commands here.

Uses plain REST via ``urllib`` (matching ``wit/desks/market_intel.py``'s existing
Finnhub-fetch pattern) rather than an unverified "massive-com/client-python"
package name — reasonable since the free tier is REST-polled anyway, so there is
no WebSocket client to gain from an official SDK. Every request, whether a
one-off historical fetch or a live polling loop, goes through one shared
``_RateLimiter`` instance so total Polygon call volume across every subscribed
symbol never exceeds the account's confirmed 5-requests/minute ceiling
(``PolygonDataClientConfig.max_requests_per_minute``) — under contention this
means slower per-symbol refresh, not a burst that gets rate-limited into
outright failures.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections import deque

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.data.messages import (
    RequestBars,
    SubscribeBars,
    SubscribeQuoteTicks,
    UnsubscribeBars,
    UnsubscribeQuoteTicks,
)
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import BarAggregation
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.objects import Price, Quantity

from wit.adapters.polygon.config import PolygonDataClientConfig

_PRICE_PRECISION = 2  # matches wit/adapters/alpaca/providers.py's Equity precision
_POLYGON_TIMESPAN: dict[BarAggregation, str] = {
    BarAggregation.MINUTE: "minute",
    BarAggregation.HOUR: "hour",
    BarAggregation.DAY: "day",
}


class _RateLimiter:
    """A rolling-window (not fixed-bucket) limiter: at most ``max_per_minute``
    calls in any trailing 60s, shared by every caller so contention delays
    requests rather than letting any single poller exceed the account limit."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self._max:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0]) + 0.25
                await asyncio.sleep(wait)


_PolygonError = (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError)


def _polygon_get(path: str, api_key: str, **params: str) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://api.polygon.io{path}?{query}&apiKey={api_key}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _bar_seconds(bar_type: BarType) -> int:
    spec = bar_type.spec
    unit = {"minute": 60, "hour": 3600, "day": 86400}[_POLYGON_TIMESPAN[spec.aggregation]]
    return spec.step * unit


def _agg_to_bar(bar_type: BarType, agg: dict, ts_init: int) -> Bar:
    duration_ns = _bar_seconds(bar_type) * 1_000_000_000
    ts_event = agg["t"] * 1_000_000 + duration_ns  # Polygon's `t` is the bar's OPEN ms-epoch
    return Bar(
        bar_type=bar_type,
        open=Price(agg["o"], _PRICE_PRECISION),
        high=Price(agg["h"], _PRICE_PRECISION),
        low=Price(agg["l"], _PRICE_PRECISION),
        close=Price(agg["c"], _PRICE_PRECISION),
        volume=Quantity.from_int(int(agg.get("v", 0))),
        ts_event=ts_event,
        ts_init=ts_init,
    )


class PolygonDataClient(LiveMarketDataClient):
    """REST-polled bar/quote data client for Polygon/Massive.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    name : str
        The client ID this instance is registered under (``TradingNodeConfig.data_clients``
        key) - conventionally "POLYGON", distinct from "ALPACA" since this is a
        separate client_id routed to the ALPACA venue (see this module's docstring).
    instrument_provider : InstrumentProvider
        The (shared, Alpaca-backed) instrument provider - see
        ``wit/adapters/alpaca/factories.py``'s caching, which hands the same
        instance to both adapters.
    config : PolygonDataClientConfig
        The client configuration.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        name: str,
        instrument_provider: InstrumentProvider,
        config: PolygonDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name),
            venue=None,  # multi-venue capable: routed to ALPACA via RoutingConfig (see node_live.py)
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._api_key = config.api_key
        self._poll_interval = config.poll_interval_secs
        self._limiter = _RateLimiter(config.max_requests_per_minute)
        self._bar_tasks: dict[BarType, asyncio.Task] = {}
        self._quote_tasks: dict = {}

    async def _connect(self) -> None:
        await self.instrument_provider.initialize()
        for instrument in self.instrument_provider.list_all():
            self._handle_data(instrument)

    async def _disconnect(self) -> None:
        for task in list(self._bar_tasks.values()) + list(self._quote_tasks.values()):
            task.cancel()
        self._bar_tasks.clear()
        self._quote_tasks.clear()
        await self.cancel_pending_tasks()

    async def _fetch_aggs(
        self, symbol: str, multiplier: int, timespan: str,
        start_ms: int, end_ms: int, limit: int = 5000,
    ) -> list[dict]:
        await self._limiter.acquire()
        try:
            data = await asyncio.to_thread(
                _polygon_get,
                f"/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{start_ms}/{end_ms}",
                self._api_key,
                adjusted="true", sort="asc", limit=str(limit),
            )
        except _PolygonError as e:
            self._log.error(f"Polygon aggs request failed for {symbol}: {type(e).__name__}: {e}")
            return []
        if data.get("status") not in ("OK", "DELAYED"):
            self._log.error(f"Polygon aggs error for {symbol}: {data}")
            return []
        return data.get("results", []) or []

    # -- historical bars (request_bars, strategy.py's warmup) -------------------
    async def _request_bars(self, request: RequestBars) -> None:
        bar_type = request.bar_type
        symbol = bar_type.instrument_id.symbol.value
        multiplier = bar_type.spec.step
        timespan = _POLYGON_TIMESPAN.get(bar_type.spec.aggregation)
        if timespan is None:
            self._log.error(f"Cannot request {bar_type} bars: unsupported aggregation for Polygon")
            self._handle_bars(bar_type, [], request.id, request.start, request.end, request.params)
            return

        start_ms = int(request.start.timestamp() * 1000) if request.start else 0
        end_ms = int(request.end.timestamp() * 1000) if request.end else int(time.time() * 1000)
        results = await self._fetch_aggs(symbol, multiplier, timespan, start_ms, end_ms)

        ts_init = self._clock.timestamp_ns()
        bars = [_agg_to_bar(bar_type, agg, ts_init) for agg in results]
        if request.limit and len(bars) > request.limit:
            bars = bars[-request.limit:]

        self._handle_bars(bar_type, bars, request.id, request.start, request.end, request.params)

    # -- live bars (polled) ------------------------------------------------------
    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        bar_type = command.bar_type
        if bar_type in self._bar_tasks:
            return
        self._bar_tasks[bar_type] = self.create_task(
            self._poll_bars(bar_type), log_msg=f"polygon_poll_bars:{bar_type}",
        )

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        task = self._bar_tasks.pop(command.bar_type, None)
        if task is not None:
            task.cancel()

    async def _poll_bars(self, bar_type: BarType) -> None:
        symbol = bar_type.instrument_id.symbol.value
        multiplier = bar_type.spec.step
        timespan = _POLYGON_TIMESPAN.get(bar_type.spec.aggregation)
        if timespan is None:
            self._log.error(f"Cannot poll {bar_type}: unsupported aggregation for Polygon")
            return

        last_emitted_ms = 0
        while True:
            now_ms = int(time.time() * 1000)
            lookback_ms = _bar_seconds(bar_type) * 1000 * 3
            results = await self._fetch_aggs(
                symbol, multiplier, timespan, now_ms - lookback_ms, now_ms, limit=5,
            )
            ts_init = self._clock.timestamp_ns()
            for agg in results:
                if agg["t"] <= last_emitted_ms:
                    continue
                last_emitted_ms = agg["t"]
                self._handle_data(_agg_to_bar(bar_type, agg, ts_init))
            await asyncio.sleep(self._poll_interval)

    # -- live quotes (polled, synthetic - see module docstring on the free tier's
    #    lack of real NBBO) -------------------------------------------------------
    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        instrument_id = command.instrument_id
        if instrument_id in self._quote_tasks:
            return
        self._quote_tasks[instrument_id] = self.create_task(
            self._poll_quotes(instrument_id), log_msg=f"polygon_poll_quotes:{instrument_id}",
        )

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        task = self._quote_tasks.pop(command.instrument_id, None)
        if task is not None:
            task.cancel()

    async def _poll_quotes(self, instrument_id) -> None:
        """Free tier has no real bid/ask (last-quote/NBBO endpoints return 403
        NOT_AUTHORIZED, confirmed in Phase 0) - publishes the latest 1-minute
        aggregate's close as a synthetic bid==ask, an honest degradation rather
        than a fabricated spread. `strategy.py`'s spread gate reads this as a
        zero-spread quote, which only ever makes the spread gate MORE permissive,
        never silently blocks a trade that should have gone through."""
        symbol = instrument_id.symbol.value
        while True:
            now_ms = int(time.time() * 1000)
            results = await self._fetch_aggs(symbol, 1, "minute", now_ms - 5 * 60_000, now_ms, limit=1)
            if results:
                agg = results[-1]
                price = Price(agg["c"], _PRICE_PRECISION)
                tick = QuoteTick(
                    instrument_id=instrument_id,
                    bid_price=price, ask_price=price,
                    bid_size=Quantity.from_int(0), ask_size=Quantity.from_int(0),
                    ts_event=agg["t"] * 1_000_000, ts_init=self._clock.timestamp_ns(),
                )
                self._handle_data(tick)
            await asyncio.sleep(self._poll_interval)
