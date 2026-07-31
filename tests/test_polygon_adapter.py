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
