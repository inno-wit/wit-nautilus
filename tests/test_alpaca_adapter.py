"""wit/adapters/alpaca/: instrument conversion and order/report mapping logic
- the pure-Python translation layer between Alpaca's REST/WS shapes and
Nautilus's, exercised without any real network call (fake stand-ins for
alpaca-py's TradingClient/Asset/Order objects, matching the plan's "mock-server
tests first" discipline). A real connected run is Phase 7's staged validation
gate, not something a unit test does.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from wit.adapters.alpaca.common import ALPACA_VENUE
from wit.adapters.alpaca.config import AlpacaInstrumentProviderConfig
from wit.adapters.alpaca.providers import AlpacaInstrumentProvider, _asset_to_equity

# ── common ───────────────────────────────────────────────────────────────

def test_alpaca_venue_is_named_alpaca():
    assert str(ALPACA_VENUE) == "ALPACA"


# ── providers: Asset -> Equity conversion ───────────────────────────────

@dataclass
class _FakeAsset:
    id: str
    symbol: str
    exchange: str
    tradable: bool


def test_asset_to_equity_uses_the_alpaca_venue():
    asset = _FakeAsset(id="abc-123", symbol="AAPL", exchange="NASDAQ", tradable=True)
    equity = _asset_to_equity(asset, ts_now=123)
    assert str(equity.id) == "AAPL.ALPACA"
    assert equity.id.venue == ALPACA_VENUE


def test_asset_to_equity_whole_share_lot_size():
    """Phase 1's watchlist check confirmed all seven current symbols are
    plain NASDAQ common stock - lot_size=1 matches what the risk sizing
    layer will ever submit, not what Alpaca could theoretically accept."""
    asset = _FakeAsset(id="abc-123", symbol="NVDA", exchange="NASDAQ", tradable=True)
    equity = _asset_to_equity(asset, ts_now=123)
    assert int(equity.lot_size) == 1
    assert equity.price_precision == 2


class _FakeTradingClient:
    def __init__(self, assets: dict[str, _FakeAsset], all_assets: list[_FakeAsset] | None = None):
        self._assets = assets
        self._all_assets = all_assets or list(assets.values())
        self.get_asset_calls: list[str] = []

    def get_asset(self, symbol: str) -> _FakeAsset:
        self.get_asset_calls.append(symbol)
        if symbol not in self._assets:
            raise ValueError(f"unknown asset {symbol}")
        return self._assets[symbol]

    def get_all_assets(self, request=None) -> list[_FakeAsset]:
        return self._all_assets


class _FakeClock:
    def timestamp_ns(self) -> int:
        return 999


def test_load_ids_async_loads_only_the_requested_tradable_symbols():
    from nautilus_trader.model.identifiers import InstrumentId

    assets = {
        "NVDA": _FakeAsset(id="1", symbol="NVDA", exchange="NASDAQ", tradable=True),
        "AAPL": _FakeAsset(id="2", symbol="AAPL", exchange="NASDAQ", tradable=True),
    }
    client = _FakeTradingClient(assets)
    provider = AlpacaInstrumentProvider(
        trading_client=client, clock=_FakeClock(),
        config=AlpacaInstrumentProviderConfig(),
    )
    asyncio.run(provider.load_ids_async(
        [InstrumentId.from_str("NVDA.ALPACA"), InstrumentId.from_str("AAPL.ALPACA")],
    ))
    loaded = {str(i) for i in provider.get_all()}
    assert loaded == {"NVDA.ALPACA", "AAPL.ALPACA"}
    assert sorted(client.get_asset_calls) == ["AAPL", "NVDA"]


def test_load_ids_async_skips_a_non_tradable_asset():
    from nautilus_trader.model.identifiers import InstrumentId

    assets = {"NVDA": _FakeAsset(id="1", symbol="NVDA", exchange="NASDAQ", tradable=False)}
    client = _FakeTradingClient(assets)
    provider = AlpacaInstrumentProvider(
        trading_client=client, clock=_FakeClock(),
        config=AlpacaInstrumentProviderConfig(),
    )
    asyncio.run(provider.load_ids_async([InstrumentId.from_str("NVDA.ALPACA")]))
    assert provider.get_all() == {}


def test_load_ids_async_skips_an_unresolvable_symbol_without_blocking_the_rest():
    """One unresolvable symbol must not block the rest of the watchlist -
    mirrors _finnhub_fetch's per-endpoint isolation in market_intel.py."""
    from nautilus_trader.model.identifiers import InstrumentId

    assets = {"NVDA": _FakeAsset(id="1", symbol="NVDA", exchange="NASDAQ", tradable=True)}
    client = _FakeTradingClient(assets)
    provider = AlpacaInstrumentProvider(
        trading_client=client, clock=_FakeClock(),
        config=AlpacaInstrumentProviderConfig(),
    )
    asyncio.run(provider.load_ids_async([
        InstrumentId.from_str("NVDA.ALPACA"), InstrumentId.from_str("BOGUS.ALPACA"),
    ]))
    assert {str(i) for i in provider.get_all()} == {"NVDA.ALPACA"}


def test_load_all_async_filters_to_tradable_assets_only():
    all_assets = [
        _FakeAsset(id="1", symbol="NVDA", exchange="NASDAQ", tradable=True),
        _FakeAsset(id="2", symbol="DELISTED", exchange="NASDAQ", tradable=False),
    ]
    client = _FakeTradingClient({}, all_assets=all_assets)
    provider = AlpacaInstrumentProvider(
        trading_client=client, clock=_FakeClock(),
        config=AlpacaInstrumentProviderConfig(),
    )
    asyncio.run(provider.load_all_async())
    assert {str(i) for i in provider.get_all()} == {"NVDA.ALPACA"}


# ── execution: status/report mapping ────────────────────────────────────

def test_alpaca_status_map_covers_every_documented_alpaca_order_status():
    from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus

    from wit.adapters.alpaca.execution import _ALPACA_STATUS_TO_NAUTILUS

    for status in AlpacaOrderStatus:
        assert status.value in _ALPACA_STATUS_TO_NAUTILUS, f"unmapped Alpaca status: {status.value}"


def test_alpaca_type_map_covers_every_documented_alpaca_order_type():
    from alpaca.trading.enums import OrderType as AlpacaOrderType

    from wit.adapters.alpaca.execution import _ALPACA_TYPE_TO_NAUTILUS

    for otype in AlpacaOrderType:
        assert otype.value in _ALPACA_TYPE_TO_NAUTILUS, f"unmapped Alpaca order type: {otype.value}"


def test_instrument_id_for_uses_the_alpaca_venue():
    from wit.adapters.alpaca.execution import _instrument_id_for

    assert str(_instrument_id_for("NVDA")) == "NVDA.ALPACA"


def test_order_to_report_maps_a_filled_order():
    from wit.adapters.alpaca.execution import AlpacaExecutionClient

    fake_order = SimpleNamespace(
        id="order-1", client_order_id="O-123", symbol="NVDA",
        side=SimpleNamespace(value="buy"), type=SimpleNamespace(value="market"),
        time_in_force=SimpleNamespace(value="gtc"), status=SimpleNamespace(value="filled"),
        qty="10", filled_qty="10", limit_price=None, stop_price=None,
        filled_avg_price="123.45",
    )

    # _order_to_report is a plain instance method with no I/O - call it via
    # __func__ against a minimal stand-in rather than constructing a full
    # AlpacaExecutionClient (which needs a live asyncio loop/msgbus/cache).
    class _Stub:
        account_id = "ALPACA-PA1"
        _clock = _FakeClock()
        _order_to_report = AlpacaExecutionClient._order_to_report

    report = _Stub()._order_to_report(fake_order)
    assert str(report.instrument_id) == "NVDA.ALPACA"
    assert str(report.venue_order_id) == "order-1"
    assert str(report.client_order_id) == "O-123"
    assert report.avg_px == Decimal("123.45")
    from nautilus_trader.model.enums import OrderStatus as NautilusOrderStatus
    assert report.order_status == NautilusOrderStatus.FILLED
