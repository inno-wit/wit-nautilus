"""``AlpacaInstrumentProvider`` — resolves the watchlist's tradable US-equity
assets via Alpaca's ``TradingClient.get_all_assets``/``get_asset`` and converts
them to Nautilus ``Equity`` instruments, all under ``ALPACA_VENUE`` (see
``common.py``'s module docstring for why a single venue is used regardless of
where bar data actually comes from).

Precision: Alpaca's paper equities are whole-share, cent-priced (Phase 1's
watchlist overlap check confirmed all seven current symbols are plain NASDAQ
common stock, not fractional-only issues) — ``price_precision=2``,
``price_increment=0.01``, ``lot_size=1``. ``Asset.min_trade_increment`` isn't
used to derive a fractional ``lot_size``: the risk sizing layer
(``wit/risk/sizing.py``, ported from the MT5 build) already rounds to whole
units, so lot_size=1 matches what this system will ever submit, not what
Alpaca could theoretically accept.
"""
from __future__ import annotations

from nautilus_trader.common.component import Clock
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import Equity, Instrument
from nautilus_trader.model.objects import Currency, Price, Quantity

from wit.adapters.alpaca.common import ALPACA_VENUE
from wit.adapters.alpaca.config import AlpacaInstrumentProviderConfig

_PRICE_PRECISION = 2
_PRICE_INCREMENT = Price(0.01, _PRICE_PRECISION)
_LOT_SIZE = Quantity.from_int(1)


def _asset_to_equity(asset, ts_now: int) -> Equity:
    instrument_id = InstrumentId(Symbol(asset.symbol), ALPACA_VENUE)
    return Equity(
        instrument_id=instrument_id,
        raw_symbol=Symbol(asset.symbol),
        currency=Currency.from_str("USD"),
        price_precision=_PRICE_PRECISION,
        price_increment=_PRICE_INCREMENT,
        lot_size=_LOT_SIZE,
        ts_event=ts_now,
        ts_init=ts_now,
        info={"asset_id": str(asset.id), "exchange": str(asset.exchange)},
    )


class AlpacaInstrumentProvider(InstrumentProvider):
    """Loads tradable US-equity ``Equity`` instruments from Alpaca.

    Parameters
    ----------
    trading_client : alpaca.trading.client.TradingClient
        The Alpaca trading REST client (shared with ``AlpacaExecutionClient`` -
        one client per process, per Alpaca's own connection-pooling guidance).
    clock : Clock
        The clock used to timestamp loaded instruments.
    config : AlpacaInstrumentProviderConfig
        The provider configuration.

    """

    def __init__(
        self,
        trading_client,
        clock: Clock,
        config: AlpacaInstrumentProviderConfig,
    ) -> None:
        super().__init__(config=config)
        self._client = trading_client
        self._clock = clock

    async def load_all_async(self, filters: dict | None = None) -> None:
        """Load every tradable US-equity asset Alpaca's paper account can see.

        Runs in a thread since alpaca-py's ``TradingClient`` is a synchronous
        (requests-based) client, not asyncio-native - matches how
        ``AlpacaExecutionClient`` calls it elsewhere in this adapter."""
        import asyncio

        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        request = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
        assets = await asyncio.to_thread(self._client.get_all_assets, request)

        ts_now = self._clock.timestamp_ns()
        instruments: list[Instrument] = [
            _asset_to_equity(asset, ts_now) for asset in assets if asset.tradable
        ]
        self.add_bulk(instruments)

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """Per-symbol fetch via ``get_asset`` — cheaper than ``load_all_async``'s
        bulk pull for the watchlist's small, fixed symbol set (overrides the
        base class's "load everything, then filter" default, which the parent
        docstring explicitly invites subclasses with per-instrument fetch
        capability to do)."""
        import asyncio

        if not instrument_ids:
            return

        ts_now = self._clock.timestamp_ns()
        instruments: list[Instrument] = []
        for instrument_id in instrument_ids:
            symbol = instrument_id.symbol.value
            try:
                asset = await asyncio.to_thread(self._client.get_asset, symbol)
            except Exception as e:  # noqa: BLE001 - surfaced via the log, not a raise:
                # one unresolvable symbol must not block the rest of the watchlist.
                self._log.error(f"Could not load Alpaca asset {symbol}: {type(e).__name__}: {e}")
                continue
            if not asset.tradable:
                self._log.warning(f"Alpaca asset {symbol} is not tradable, skipping")
                continue
            instruments.append(_asset_to_equity(asset, ts_now))

        self.add_bulk(instruments)
