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
import random
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

# Bounds the one-time warmup fetch's page count per symbol - Polygon's aggs
# endpoint paginates (see _fetch_aggs's docstring), and unboundedly following
# next_url until request.limit (750) is satisfied would cost far more of the
# shared rate budget than warming up 7 symbols can afford at boot. 3 pages
# lands around 270-330 bars per symbol in practice (each page ≈90-110 bars on
# this account) - comfortably clears the strategy's 101-bar floor with margin
# without turning startup into a 15+ minute pagination crawl.
_MAX_WARMUP_PAGES = 3


def _polygon_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _polygon_aggs_url(
    symbol: str, multiplier: int, timespan: str, start_ms: int, end_ms: int,
    api_key: str, limit: int,
) -> str:
    return (
        f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}"
        f"/{start_ms}/{end_ms}?adjusted=true&sort=asc&limit={limit}&apiKey={api_key}"
    )


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
        # Quotes piggyback on an existing bar poller for the same instrument
        # wherever one exists (audit finding B2): `_poll_quotes`'s own 1-minute-
        # aggregate fetch fell entirely inside the free tier's ~15min delayed-
        # data entitlement window and 403'd on every single cycle - not a rate-
        # limit fluke, a structurally unsatisfiable request. Deriving the
        # synthetic quote from the SAME data `_poll_bars` already fetches
        # halves total call volume (14 pollers -> 7) and removes the failing
        # call shape outright, rather than just budgeting around it.
        self._want_quotes: dict[object, bool] = {}
        # Fallback only for an instrument that gets a quote subscription with
        # no matching bar subscription - not exercised by strategy.py today
        # (it always subscribes both together), kept for robustness.
        self._standalone_quote_tasks: dict[object, asyncio.Task] = {}

    async def _connect(self) -> None:
        # `_instrument_provider`, not `instrument_provider` (no public alias
        # exists on LiveMarketDataClient - confirmed live against a real boot
        # after this attribute name was initially guessed wrong).
        await self._instrument_provider.initialize()
        for instrument in self._instrument_provider.list_all():
            self._handle_data(instrument)

    async def _disconnect(self) -> None:
        for task in list(self._bar_tasks.values()) + list(self._standalone_quote_tasks.values()):
            task.cancel()
        self._bar_tasks.clear()
        self._want_quotes.clear()
        self._standalone_quote_tasks.clear()
        await self.cancel_pending_tasks()

    async def _fetch_aggs(
        self, symbol: str, multiplier: int, timespan: str,
        start_ms: int, end_ms: int, limit: int = 5000, max_pages: int = 1,
    ) -> list[dict]:
        """Follows Polygon's ``next_url`` cursor for up to ``max_pages`` pages.

        This is the actual root cause of audit finding B1 (6/7 symbols warmed
        up below the strategy's 101-bar floor): a prior fix attempt widened
        the requested date range on the theory that equities' ~6.5h/24h
        trading calendar was starving a calendar-time lookback window, but a
        live redeploy showed delivered counts stayed flat (83-110 bars)
        regardless of asking 9-27x further back - proving it wasn't a
        date-range depth problem. A clean local request against this account
        confirmed the real cause instead: Polygon's aggs endpoint paginates
        (``resultsCount`` far short of a 90-day window's true bar count, with
        a populated ``next_url`` in the response) and nothing here was
        following it, so every call silently returned only its first page no
        matter how wide the requested range was. Live polling (``_poll_bars``/
        ``_poll_quotes_standalone``) only ever wants the newest few bars, so
        it stays at the default ``max_pages=1``; only the one-time warmup
        fetch (``_request_bars``) pages further."""
        url = _polygon_aggs_url(symbol, multiplier, timespan, start_ms, end_ms, self._api_key, limit)
        results: list[dict] = []
        for _ in range(max_pages):
            await self._limiter.acquire()
            try:
                data = await asyncio.to_thread(_polygon_get, url)
            except urllib.error.HTTPError as e:
                # Read the response body before it's gone (audit finding M1):
                # Polygon's error JSON distinguishes NOT_AUTHORIZED (entitlement)
                # from the rate-limit message, and the request shape (multiplier/
                # timespan/window) is what actually identifies which poller failed
                # - both were being discarded, which is why a systematic per-call-
                # shape failure (see B2) read as an unexplained trickle instead.
                try:
                    body = e.read().decode(errors="replace")[:300]
                except Exception:  # noqa: BLE001 - body read is best-effort diagnostics
                    body = "<no body>"
                self._log.error(
                    f"Polygon aggs request failed for {symbol} {multiplier}/{timespan} "
                    f"[{start_ms}, {end_ms}]: HTTP {e.code}: {body}"
                )
                break
            except _PolygonError as e:
                self._log.error(
                    f"Polygon aggs request failed for {symbol} {multiplier}/{timespan} "
                    f"[{start_ms}, {end_ms}]: {type(e).__name__}: {e}"
                )
                break
            if data.get("status") not in ("OK", "DELAYED"):
                self._log.error(f"Polygon aggs error for {symbol} {multiplier}/{timespan}: {data}")
                break
            results.extend(data.get("results", []) or [])
            next_url = data.get("next_url")
            if not next_url:
                break
            url = f"{next_url}&apiKey={self._api_key}"
        return results

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
        results = await self._fetch_aggs(
            symbol, multiplier, timespan, start_ms, end_ms, max_pages=_MAX_WARMUP_PAGES,
        )

        ts_init = self._clock.timestamp_ns()
        bars = [_agg_to_bar(bar_type, agg, ts_init) for agg in results]
        if request.limit and len(bars) > request.limit:
            bars = bars[-request.limit:]
        if request.limit and len(bars) < request.limit:
            # Logged, not raised - a partial warmup still gives the strategy
            # something to work with; strategy.py's own 101-bar floor is the
            # actual gate on whether it's enough to compute on.
            self._log.warning(
                f"{symbol}: warmup delivered {len(bars)}/{request.limit} bars "
                f"(hit the {_MAX_WARMUP_PAGES}-page cap) - this account's Polygon "
                f"history for this symbol/timeframe is short of the full request"
            )

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

    def _bar_poller_running_for(self, instrument_id) -> bool:
        return any(bt.instrument_id == instrument_id for bt in self._bar_tasks)

    async def _poll_bars(self, bar_type: BarType) -> None:
        """Publishes bars AND (when a quote subscription exists for the same
        instrument, per `_want_quotes`) a synthetic quote derived from the
        SAME fetched data - audit finding B2. Quotes no longer run their own
        1-minute-aggregate poller: that call shape fell entirely inside the
        free tier's ~15min delayed-data entitlement window and 403'd on every
        single cycle, not just under rate-limit contention. Consolidating
        halves total Polygon call volume (14 pollers -> 7) and removes the
        failing shape outright rather than budgeting around it."""
        symbol = bar_type.instrument_id.symbol.value
        multiplier = bar_type.spec.step
        timespan = _POLYGON_TIMESPAN.get(bar_type.spec.aggregation)
        if timespan is None:
            self._log.error(f"Cannot poll {bar_type}: unsupported aggregation for Polygon")
            return

        # Seed the watermark from whatever warmup already delivered (audit
        # finding B4): starting at 0 made this poller's first iteration
        # re-emit the last few warmup bars a second time - Cache.add_bar has
        # no timestamp dedup, so the duplicated tail reached ATR and therefore
        # position size directly.
        existing = self._cache.bars(bar_type)
        last_emitted_ns = max((b.ts_event for b in existing), default=0)

        # Jittered start (audit finding M2): without this, every poller for
        # every symbol/subscription starts within about a second of each
        # other at boot and stays phase-locked every cycle thereafter -
        # unnecessary synchronized burst pressure on top of the rate limiter.
        await asyncio.sleep(random.uniform(0, self._poll_interval))

        while True:
            now_ms = int(time.time() * 1000)
            lookback_ms = _bar_seconds(bar_type) * 1000 * 3
            results = await self._fetch_aggs(
                symbol, multiplier, timespan, now_ms - lookback_ms, now_ms, limit=5,
            )
            ts_init = self._clock.timestamp_ns()
            for agg in results:
                bar = _agg_to_bar(bar_type, agg, ts_init)
                if bar.ts_event > last_emitted_ns:
                    last_emitted_ns = bar.ts_event
                    self._handle_data(bar)

            if results and self._want_quotes.get(bar_type.instrument_id):
                self._publish_synthetic_quote(bar_type.instrument_id, results[-1], ts_init)

            await asyncio.sleep(self._poll_interval)

    def _publish_synthetic_quote(self, instrument_id, agg: dict, ts_init: int) -> None:
        price = Price(agg["c"], _PRICE_PRECISION)
        tick = QuoteTick(
            instrument_id=instrument_id,
            bid_price=price, ask_price=price,
            bid_size=Quantity.from_int(0), ask_size=Quantity.from_int(0),
            ts_event=agg["t"] * 1_000_000, ts_init=ts_init,
        )
        self._handle_data(tick)

    # -- live quotes (polled, synthetic - see module docstring on the free tier's
    #    lack of real NBBO) -------------------------------------------------------
    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        instrument_id = command.instrument_id
        if self._bar_poller_running_for(instrument_id):
            # Piggyback on the existing bar poller (see _poll_bars) instead of
            # starting a second, independent one - this is the path
            # strategy.py always takes (subscribe_bars then
            # subscribe_quote_ticks together in _on_warmup_complete).
            self._want_quotes[instrument_id] = True
            return
        if instrument_id in self._standalone_quote_tasks:
            return
        self._standalone_quote_tasks[instrument_id] = self.create_task(
            self._poll_quotes_standalone(instrument_id),
            log_msg=f"polygon_poll_quotes:{instrument_id}",
        )

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        instrument_id = command.instrument_id
        self._want_quotes.pop(instrument_id, None)
        task = self._standalone_quote_tasks.pop(instrument_id, None)
        if task is not None:
            task.cancel()

    async def _poll_quotes_standalone(self, instrument_id) -> None:
        """Fallback path for a quote subscription with no matching bar
        subscription - not exercised by strategy.py today (see
        `_subscribe_quote_ticks`), kept for robustness. Uses the same
        ascending-sort, take-the-last-result pattern `_poll_bars` uses rather
        than `limit=1` (audit finding B3): with `sort="asc"` and `limit=1`,
        Polygon returns the OLDEST bar in the window, not the newest -
        `results[-1]` on a one-element list is a no-op, so the original
        version's synthetic quote was up to five minutes stale before the
        tier's own delay was even counted."""
        symbol = instrument_id.symbol.value
        await asyncio.sleep(random.uniform(0, self._poll_interval))
        while True:
            now_ms = int(time.time() * 1000)
            results = await self._fetch_aggs(symbol, 1, "minute", now_ms - 5 * 60_000, now_ms, limit=5)
            if results:
                self._publish_synthetic_quote(instrument_id, results[-1], self._clock.timestamp_ns())
            await asyncio.sleep(self._poll_interval)
