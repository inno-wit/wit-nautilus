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
from datetime import UTC
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


# ── _publish_account_state: the AccountBalance invariant (audit finding H1/H3) ─

def _account_state_for(equity: str, cash: str, buying_power: str) -> dict:
    """Drives AlpacaExecutionClient._publish_account_state against a fake
    Alpaca account and captures the AccountBalance it builds, without
    constructing a full live client (no asyncio loop/msgbus/cache needed -
    generate_account_state is stubbed out to just record its arguments)."""
    from wit.adapters.alpaca.execution import AlpacaExecutionClient

    captured = {}

    class _Stub:
        _clock = _FakeClock()
        _publish_account_state = AlpacaExecutionClient._publish_account_state

        def generate_account_state(self, balances, margins, reported, ts_event):
            captured["balance"] = balances[0]

    account = SimpleNamespace(equity=equity, cash=cash, buying_power=buying_power)
    _Stub()._publish_account_state(account)
    return captured["balance"]


def test_account_balance_invariant_holds_on_a_normal_long_only_account():
    balance = _account_state_for(equity="10000", cash="8000", buying_power="8000")
    assert balance.total.as_decimal() - balance.locked.as_decimal() == balance.free.as_decimal()


def test_account_balance_invariant_holds_when_cash_exceeds_equity():
    """A short position makes Alpaca's own cash > equity (short market value
    is negative) - the original `locked = max(equity - cash, 0)` mapping
    clamped locked to 0 in this case while free stayed at cash, breaking
    AccountBalance's hard total-locked==free assertion and crashing
    _connect/QueryAccount outright (audit finding H1)."""
    balance = _account_state_for(equity="9000", cash="9500", buying_power="7000")
    assert balance.total.as_decimal() - balance.locked.as_decimal() == balance.free.as_decimal()
    assert balance.locked.as_decimal() >= 0


def test_account_balance_free_reflects_buying_power_not_bare_cash():
    """Audit finding H3: mapping `free` from cash meant it could go negative
    on a margin account well before buying power did, silently freezing the
    sizing gate with no alert. `free` must track buying_power."""
    balance = _account_state_for(equity="10000", cash="-500", buying_power="6000")
    assert balance.free.as_decimal() == Decimal(6000)
    assert balance.locked.as_decimal() >= 0


def test_account_balance_free_never_exceeds_equity_even_with_excess_buying_power():
    """Margin buying power can exceed equity (leverage) - free must still be
    clamped so the AccountBalance invariant holds."""
    balance = _account_state_for(equity="5000", cash="5000", buying_power="20000")
    assert balance.free.as_decimal() <= balance.total.as_decimal()
    assert balance.total.as_decimal() - balance.locked.as_decimal() == balance.free.as_decimal()


# ── _parse_alpaca_time (used by generate_fill_reports, audit finding H2) ──

def test_parse_alpaca_time_handles_a_z_suffixed_timestamp():
    from datetime import datetime

    from wit.adapters.alpaca.execution import _parse_alpaca_time

    ns = _parse_alpaca_time("2026-07-31T12:00:00.000Z", _FakeClock())
    expected = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    assert ns == int(expected.timestamp() * 1_000_000_000)


def test_parse_alpaca_time_falls_back_to_the_clock_on_missing_value():
    from wit.adapters.alpaca.execution import _parse_alpaca_time

    assert _parse_alpaca_time(None, _FakeClock()) == 999


def test_parse_alpaca_time_falls_back_to_the_clock_on_unparseable_value():
    from wit.adapters.alpaca.execution import _parse_alpaca_time

    assert _parse_alpaca_time("not-a-timestamp", _FakeClock()) == 999


# ── generate_fill_reports: trade_id from the activity id, not order.id ────

def test_fill_report_trade_id_uses_the_activity_uuid_not_the_order_id():
    """Audit finding H2: the prior version used TradeId(str(order.id)) here
    while _on_trade_update uses the WebSocket's real execution_id for the
    identical fill - two different ids for the same event defeats Nautilus's
    trade_id-keyed fill dedup. Alpaca's activity id is a composite
    "<time>::<uuid>" string whose UUID segment is the same execution id the
    WebSocket sends."""
    import asyncio as _asyncio

    from wit.adapters.alpaca.execution import AlpacaExecutionClient

    activity = {
        "id": "20260731120000000::9c1c1234-5678-90ab-cdef-1234567890ab",
        "order_id": "order-1", "symbol": "NVDA", "side": "buy",
        "qty": "10", "price": "123.45", "transaction_time": "2026-07-31T12:00:00Z",
    }

    class _Stub:
        account_id = "ALPACA-PA1"
        _clock = _FakeClock()
        _log = SimpleNamespace(error=lambda *a, **kw: None, info=lambda *a, **kw: None)
        generate_fill_reports = AlpacaExecutionClient.generate_fill_reports

        def _log_report_receipt(self, *a, **kw):
            pass

        class _client:
            @staticmethod
            def get(path, params):
                return [activity]

    class _Command:
        instrument_id = None
        start = None
        end = None

    reports = _asyncio.run(_Stub().generate_fill_reports(_Command()))
    assert len(reports) == 1
    assert str(reports[0].trade_id) == "9c1c1234-5678-90ab-cdef-1234567890ab"
