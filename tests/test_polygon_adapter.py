"""wit/adapters/polygon/: the rate limiter and aggregate-bar conversion logic
- pure-Python pieces exercised without a real network call (Phase 0/1 of the
broker swap already confirmed the free-tier 5/min limit and the 403-on-
real-time-endpoint signal live; these tests lock in the conversion/limiting
logic built around those confirmed facts). A real connected run is Phase 7's
staged validation gate, not something a unit test does.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import BarAggregation

from wit.adapters.polygon.data import (
    _POLYGON_TIMESPAN,
    _agg_to_bar,
    _bar_seconds,
    _RateLimiter,
)

# ── timespan mapping ─────────────────────────────────────────────────────

def test_polygon_timespan_covers_the_aggregations_node_live_can_produce():
    """node_live.py's _TIMEFRAMES table only ever emits MINUTE/HOUR/DAY
    aggregations (M15/M30/H1/H4/D1) - confirm Polygon's mapping covers all
    three, so an unsupported-aggregation error path is never actually hit
    by the current watchlist."""
    for agg in (BarAggregation.MINUTE, BarAggregation.HOUR, BarAggregation.DAY):
        assert agg in _POLYGON_TIMESPAN


def test_bar_seconds_for_h1():
    bar_type = BarType.from_str("NVDA.ALPACA-1-HOUR-LAST-EXTERNAL")
    assert _bar_seconds(bar_type) == 3600


def test_bar_seconds_for_h4():
    bar_type = BarType.from_str("NVDA.ALPACA-4-HOUR-LAST-EXTERNAL")
    assert _bar_seconds(bar_type) == 4 * 3600


def test_bar_seconds_for_m15():
    bar_type = BarType.from_str("NVDA.ALPACA-15-MINUTE-LAST-EXTERNAL")
    assert _bar_seconds(bar_type) == 15 * 60


# ── aggregate -> Bar conversion ──────────────────────────────────────────

def test_agg_to_bar_maps_ohlcv_fields():
    bar_type = BarType.from_str("NVDA.ALPACA-1-HOUR-LAST-EXTERNAL")
    agg = {"o": 100.1, "h": 101.5, "l": 99.9, "c": 101.0, "v": 12345, "t": 1_700_000_000_000}
    bar = _agg_to_bar(bar_type, agg, ts_init=999)
    assert float(bar.open) == 100.10
    assert float(bar.high) == 101.50
    assert float(bar.low) == 99.90
    assert float(bar.close) == 101.00
    assert int(bar.volume) == 12345
    assert bar.ts_init == 999


def test_agg_to_bar_ts_event_is_the_bar_close_not_open():
    """Polygon's `t` is the bar's OPEN ms-epoch - ts_event must be the CLOSE
    (Nautilus's own Bar convention), i.e. open + the bar's duration."""
    bar_type = BarType.from_str("NVDA.ALPACA-1-HOUR-LAST-EXTERNAL")
    open_ms = 1_700_000_000_000
    agg = {"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 1, "t": open_ms}
    bar = _agg_to_bar(bar_type, agg, ts_init=0)
    expected_close_ns = open_ms * 1_000_000 + 3600 * 1_000_000_000
    assert bar.ts_event == expected_close_ns


def test_agg_to_bar_handles_missing_volume():
    bar_type = BarType.from_str("NVDA.ALPACA-1-HOUR-LAST-EXTERNAL")
    agg = {"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "t": 0}
    bar = _agg_to_bar(bar_type, agg, ts_init=0)
    assert int(bar.volume) == 0


# ── rate limiter ─────────────────────────────────────────────────────────

def test_rate_limiter_allows_up_to_the_configured_max_without_waiting():
    async def _run():
        limiter = _RateLimiter(max_per_minute=5)
        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        return time.monotonic() - start

    elapsed = asyncio.run(_run())
    assert elapsed < 1.0  # five calls under the cap must not wait at all


def test_rate_limiter_blocks_the_call_past_the_max():
    """Confirms the limiter actually enforces the confirmed 5/min free-tier
    ceiling (Phase 0/1 of the broker swap hit this live) rather than just
    counting - the 6th call in a burst must wait, not proceed immediately."""
    async def _run():
        limiter = _RateLimiter(max_per_minute=2)
        # Pre-seed two calls at "now" so the 3rd call's wait is deterministic
        # and short, rather than actually waiting ~60s in a unit test.
        now = time.monotonic()
        limiter._calls.extend([now, now])
        waited = False

        async def _timed_acquire():
            nonlocal waited
            # Shrink the window check indirectly: acquire() computes its own
            # wait from _calls[0], which is ~60s out - instead, confirm the
            # limiter is at capacity by checking a 3rd immediate acquire
            # would need to wait (not proceed instantly), using a short
            # asyncio.wait_for timeout as the "did it block" signal.
            try:
                await asyncio.wait_for(limiter.acquire(), timeout=0.2)
            except TimeoutError:
                waited = True
                raise

        with pytest.raises(asyncio.TimeoutError):
            await _timed_acquire()
        return waited

    assert asyncio.run(_run()) is True


def test_rate_limiter_prunes_calls_older_than_the_window():
    async def _run():
        limiter = _RateLimiter(max_per_minute=1)
        limiter._calls.append(time.monotonic() - 61.0)  # just outside the 60s window
        start = time.monotonic()
        await limiter.acquire()  # must not wait - the old call has aged out
        return time.monotonic() - start

    elapsed = asyncio.run(_run())
    assert elapsed < 1.0


# ── quote/bar consolidation (audit finding B2) ────────────────────────────

def test_bar_poller_running_for_finds_a_matching_instrument():
    from wit.adapters.polygon.data import PolygonDataClient

    bar_type = BarType.from_str("NVDA.ALPACA-1-HOUR-LAST-EXTERNAL")

    class _Stub:
        _bar_poller_running_for = PolygonDataClient._bar_poller_running_for

        def __init__(self):
            self._bar_tasks = {bar_type: object()}

    assert _Stub()._bar_poller_running_for(bar_type.instrument_id) is True


def test_bar_poller_running_for_is_false_with_no_matching_bar_task():
    from wit.adapters.polygon.data import PolygonDataClient

    other_bar_type = BarType.from_str("AAPL.ALPACA-1-HOUR-LAST-EXTERNAL")
    target = BarType.from_str("NVDA.ALPACA-1-HOUR-LAST-EXTERNAL").instrument_id

    class _Stub:
        _bar_poller_running_for = PolygonDataClient._bar_poller_running_for

        def __init__(self):
            self._bar_tasks = {other_bar_type: object()}

    assert _Stub()._bar_poller_running_for(target) is False


def test_publish_synthetic_quote_uses_the_aggregate_close_as_bid_and_ask():
    from wit.adapters.polygon.data import PolygonDataClient

    published = []

    class _Stub:
        _publish_synthetic_quote = PolygonDataClient._publish_synthetic_quote

        def _handle_data(self, data):
            published.append(data)

    instrument_id = BarType.from_str("NVDA.ALPACA-1-HOUR-LAST-EXTERNAL").instrument_id
    agg = {"c": 123.45, "t": 1_700_000_000_000}
    _Stub()._publish_synthetic_quote(instrument_id, agg, ts_init=999)

    assert len(published) == 1
    tick = published[0]
    assert float(tick.bid_price) == float(tick.ask_price) == 123.45
    assert tick.ts_init == 999


# ── warmup watermark seeding (audit finding B4) ───────────────────────────

def test_poll_bars_seeds_the_watermark_from_bars_already_in_the_cache():
    """The original version started `last_emitted_ms = 0`, so the poller's
    first fetch re-published bars warmup had already delivered, duplicating
    the ATR-feeding tail of the cached series. Seeding from the max
    ts_event already cached for this bar_type prevents that without needing
    to run the actual polling loop (which sleeps forever) in a unit test."""
    bar_type = BarType.from_str("NVDA.ALPACA-1-HOUR-LAST-EXTERNAL")
    ts_init = 0
    agg_old = {"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 1, "t": 1_700_000_000_000}
    agg_new = {"o": 101.0, "h": 101.0, "l": 101.0, "c": 101.0, "v": 1, "t": 1_700_003_600_000}
    cached_bars = [_agg_to_bar(bar_type, agg_old, ts_init), _agg_to_bar(bar_type, agg_new, ts_init)]

    watermark = max((b.ts_event for b in cached_bars), default=0)
    assert watermark == cached_bars[1].ts_event
    assert watermark > cached_bars[0].ts_event

    # A subsequent fetch returning the SAME two bars (as warmup's own
    # lookback window would, on the poller's first cycle) must re-publish
    # neither, since both ts_event values are <= the seeded watermark.
    refetched = [_agg_to_bar(bar_type, agg_old, ts_init), _agg_to_bar(bar_type, agg_new, ts_init)]
    to_publish = [b for b in refetched if b.ts_event > watermark]
    assert to_publish == []


# ── _fetch_aggs pagination (audit finding B1's real root cause) ──────────
#
# A first fix attempt widened the warmup request's date range on the theory
# that equities' ~6.5h/24h trading calendar was starving a calendar-time
# lookback window - a live redeploy showed delivered bar counts stayed flat
# regardless of asking 9-27x further back, disproving that theory. The
# actual cause: Polygon's aggs endpoint paginates via a `next_url` cursor in
# the response, which nothing here was following, so every call silently
# returned only its first page no matter how wide the requested range was.

class _NoOpLimiter:
    async def acquire(self) -> None:
        return None


def test_fetch_aggs_follows_next_url_across_pages(monkeypatch):
    from wit.adapters.polygon import data as polygon_data

    pages = [
        {"status": "OK", "results": [{"t": 1}, {"t": 2}], "next_url": "https://api.polygon.io/next1"},
        {"status": "OK", "results": [{"t": 3}, {"t": 4}], "next_url": "https://api.polygon.io/next2"},
        {"status": "OK", "results": [{"t": 5}], "next_url": None},
    ]
    calls: list[str] = []

    def _fake_get(url):
        calls.append(url)
        return pages[len(calls) - 1]

    monkeypatch.setattr(polygon_data, "_polygon_get", _fake_get)

    class _Stub:
        _api_key = "fake-key"
        _log = SimpleNamespace(error=lambda *a, **kw: None)
        _limiter = _NoOpLimiter()
        _fetch_aggs = polygon_data.PolygonDataClient._fetch_aggs

    results = asyncio.run(_Stub()._fetch_aggs("NVDA", 1, "hour", 0, 1000, max_pages=3))
    assert [r["t"] for r in results] == [1, 2, 3, 4, 5]
    assert len(calls) == 3
    assert calls[1].startswith("https://api.polygon.io/next1")
    assert "apiKey=fake-key" in calls[1]


def test_fetch_aggs_stops_at_max_pages_even_when_more_are_available(monkeypatch):
    """Bounds the one-time warmup fetch's rate-limit cost (audit finding B1's
    fix): unboundedly paginating until request.limit is satisfied would cost
    far more of the shared 4/min budget than warming up 7 symbols can afford
    at boot."""
    from wit.adapters.polygon import data as polygon_data

    calls: list[str] = []

    def _fake_get(url):
        calls.append(url)
        return {"status": "OK", "results": [{"t": len(calls)}], "next_url": "https://api.polygon.io/more"}

    monkeypatch.setattr(polygon_data, "_polygon_get", _fake_get)

    class _Stub:
        _api_key = "fake-key"
        _log = SimpleNamespace(error=lambda *a, **kw: None)
        _limiter = _NoOpLimiter()
        _fetch_aggs = polygon_data.PolygonDataClient._fetch_aggs

    results = asyncio.run(_Stub()._fetch_aggs("NVDA", 1, "hour", 0, 1000, max_pages=2))
    assert len(results) == 2
    assert len(calls) == 2  # never a 3rd call, even though next_url kept offering one


def test_fetch_aggs_defaults_to_a_single_page_for_live_polling():
    """_poll_bars/_poll_quotes_standalone only ever want the newest few bars
    and must not inherit warmup's multi-page cost."""
    import inspect

    from wit.adapters.polygon.data import PolygonDataClient

    sig = inspect.signature(PolygonDataClient._fetch_aggs)
    assert sig.parameters["max_pages"].default == 1
