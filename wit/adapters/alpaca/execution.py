"""``AlpacaExecutionClient`` — order submission/lifecycle against Alpaca's paper
trading API, execution only (no data — see ``common.py``'s module docstring for
the venue-sharing design).

Bracket orders map directly onto Alpaca's native ``order_class=BRACKET``: Nautilus's
``OrderFactory.bracket()`` (the only submission path ``wit/nautilus/strategy.py``
uses, per its own module docstring) always emits ``orders=[entry_order, sl_order,
tp_order]`` in that fixed order (confirmed against the installed
``nautilus_trader==1.230.0``'s ``common/factories.pyx``) with
``ContingencyType.OUO`` (one-updates-other) — Alpaca's bracket ``take_profit``/
``stop_loss`` legs already implement exactly that one-cancels-other relationship
server-side, so this client submits ONE Alpaca order per bracket rather than
managing the OCO relationship itself.

Live order/fill events arrive over Alpaca's ``TradingStream`` WebSocket
(``_on_trade_update``), not polling — ``TradingStream.run()`` wraps
``asyncio.run(...)``, which would open a second event loop and deadlock against
Nautilus's own running loop, so this client drives the stream's
``_run_forever()`` coroutine directly as a task on Nautilus's existing loop
instead (confirmed against the installed ``alpaca-py``'s source: ``_run_forever``
captures ``asyncio.get_running_loop()`` on entry, so this works correctly
whichever loop schedules it).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    BatchCancelOrders,
    CancelAllOrders,
    CancelOrder,
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    ModifyOrder,
    QueryAccount,
    SubmitOrder,
    SubmitOrderList,
)
from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.enums import (
    OrderStatus as NautilusOrderStatus,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    Symbol,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import AccountBalance, Currency, Money, Price, Quantity

from wit.adapters.alpaca.common import ALPACA_VENUE
from wit.adapters.alpaca.config import AlpacaExecClientConfig
from wit.adapters.alpaca.providers import AlpacaInstrumentProvider

_USD = Currency.from_str("USD")

# Alpaca order.status -> Nautilus OrderStatus. Statuses this system's paper flow
# is never expected to see (e.g. options-only ``accepted_for_bidding``) are still
# mapped defensively rather than raising - an unrecognized status must not crash
# reconciliation, only fail to advance state as precisely as a known one would.
_ALPACA_STATUS_TO_NAUTILUS: dict[str, NautilusOrderStatus] = {
    "new": NautilusOrderStatus.ACCEPTED,
    "accepted": NautilusOrderStatus.ACCEPTED,
    "pending_new": NautilusOrderStatus.SUBMITTED,
    "accepted_for_bidding": NautilusOrderStatus.ACCEPTED,
    "partially_filled": NautilusOrderStatus.PARTIALLY_FILLED,
    "filled": NautilusOrderStatus.FILLED,
    "done_for_day": NautilusOrderStatus.ACCEPTED,
    "canceled": NautilusOrderStatus.CANCELED,
    "expired": NautilusOrderStatus.EXPIRED,
    "replaced": NautilusOrderStatus.ACCEPTED,
    "pending_cancel": NautilusOrderStatus.PENDING_CANCEL,
    "pending_replace": NautilusOrderStatus.PENDING_UPDATE,
    "rejected": NautilusOrderStatus.REJECTED,
    "suspended": NautilusOrderStatus.ACCEPTED,
    "calculated": NautilusOrderStatus.ACCEPTED,
    "held": NautilusOrderStatus.ACCEPTED,
    "stopped": NautilusOrderStatus.ACCEPTED,
    "pending_review": NautilusOrderStatus.ACCEPTED,
}

_ALPACA_TYPE_TO_NAUTILUS: dict[str, OrderType] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop": OrderType.STOP_MARKET,
    "stop_limit": OrderType.STOP_LIMIT,
    "trailing_stop": OrderType.TRAILING_STOP_MARKET,
}


def _instrument_id_for(symbol: str) -> InstrumentId:
    return InstrumentId(Symbol(symbol), ALPACA_VENUE)


def _price_or_none(value) -> Price | None:
    return Price.from_str(str(value)) if value is not None else None


def _qty(value) -> Quantity:
    return Quantity.from_str(str(value))


class AlpacaExecutionClient(LiveExecutionClient):
    """Execution client for Alpaca's paper trading REST + trade-update WebSocket
    (``alpaca.trading.client.TradingClient`` / ``alpaca.trading.stream.TradingStream``).

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    trading_client : alpaca.trading.client.TradingClient
        The Alpaca REST client (shared with ``AlpacaInstrumentProvider``).
    trading_stream : alpaca.trading.stream.TradingStream
        The Alpaca trade-update WebSocket client.
    instrument_provider : AlpacaInstrumentProvider
        The Alpaca instrument provider.
    config : AlpacaExecClientConfig
        The execution client configuration.
    name : str
        The client ID this instance is registered under (``TradingNodeConfig.exec_clients``
        key) - must equal ``account_id.get_issuer()`` per Nautilus's own invariant,
        so this is always "ALPACA" in practice (see ``node_live.py``).
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
        trading_client,
        trading_stream,
        instrument_provider: AlpacaInstrumentProvider,
        config: AlpacaExecClientConfig,
        name: str,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name),
            venue=ALPACA_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=_USD,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._client = trading_client
        self._stream = trading_stream
        self._stream_task: asyncio.Task | None = None

    # -- connection ----------------------------------------------------------
    async def _connect(self) -> None:
        # Instruments are published to the cache by PolygonDataClient (a
        # LiveMarketDataClient, which has `_handle_data`; LiveExecutionClient does
        # not) even though this provider is what actually defines them - see
        # `common.py`'s module docstring. Still initialize the shared provider
        # here too: whichever client connects first does the real work, the
        # other's call is a no-op (`InstrumentProvider.initialize` short-circuits
        # once `_loaded` is True), and this client must not assume connection order.
        # `_instrument_provider`, not `instrument_provider` (no public alias
        # exists on LiveExecutionClient - confirmed live against a real boot
        # after this attribute name was initially guessed wrong).
        await self._instrument_provider.initialize()

        account = await asyncio.to_thread(self._client.get_account)
        self._set_account_id(AccountId(f"ALPACA-{account.account_number}"))
        self._publish_account_state(account)

        self._stream.subscribe_trade_updates(self._on_trade_update)
        self._stream_task = self.create_task(
            self._stream._run_forever(),
            log_msg="alpaca_trade_stream",
        )

    async def _disconnect(self) -> None:
        if self._stream_task is not None and not self._stream_task.done():
            await self._stream.close()

    def _publish_account_state(self, account) -> None:
        equity = Decimal(str(account.equity))
        cash = Decimal(str(account.cash))
        locked = max(equity - cash, Decimal(0))
        balance = AccountBalance(
            total=Money(equity, _USD),
            locked=Money(locked, _USD),
            free=Money(cash, _USD),
        )
        self.generate_account_state(
            balances=[balance],
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
        )

    # -- order submission ------------------------------------------------------
    async def _submit_order(self, command: SubmitOrder) -> None:
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        order = command.order
        side = "buy" if order.side == OrderSide.BUY else "sell"
        try:
            if order.order_type == OrderType.MARKET:
                request = MarketOrderRequest(
                    symbol=order.instrument_id.symbol.value,
                    qty=str(order.quantity),
                    side=side,
                    time_in_force="gtc",
                    client_order_id=str(order.client_order_id),
                )
            elif order.order_type == OrderType.LIMIT:
                request = LimitOrderRequest(
                    symbol=order.instrument_id.symbol.value,
                    qty=str(order.quantity),
                    side=side,
                    time_in_force="gtc",
                    limit_price=str(order.price),
                    client_order_id=str(order.client_order_id),
                )
            else:
                self.generate_order_rejected(
                    strategy_id=order.strategy_id,
                    instrument_id=order.instrument_id,
                    client_order_id=order.client_order_id,
                    reason=f"unsupported order_type for direct submission: {order.order_type}",
                    ts_event=self._clock.timestamp_ns(),
                )
                return

            response = await asyncio.to_thread(self._client.submit_order, request)
        except Exception as e:  # noqa: BLE001 - venue rejection must reach the strategy as an event
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=f"{type(e).__name__}: {e}",
                ts_event=self._clock.timestamp_ns(),
            )
            return

        now = self._clock.timestamp_ns()
        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=now,
        )
        self.generate_order_accepted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=VenueOrderId(str(response.id)),
            ts_event=now,
        )

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        orders = command.order_list.orders
        if len(orders) != 3:
            self._log.error(
                f"AlpacaExecutionClient only supports 3-leg brackets from "
                f"OrderFactory.bracket(); got {len(orders)} orders in {command.order_list}",
            )
            for o in orders:
                self.generate_order_rejected(
                    strategy_id=o.strategy_id,
                    instrument_id=o.instrument_id,
                    client_order_id=o.client_order_id,
                    reason="unsupported order_list shape (expected 3-leg bracket)",
                    ts_event=self._clock.timestamp_ns(),
                )
            return

        # OrderFactory.bracket() always emits [entry, sl_order, tp_order] in this
        # order (build plan cross-reference, confirmed against installed
        # nautilus_trader's common/factories.pyx) - not re-derived by type here,
        # since STOP_LIMIT sl_order_type or LIMIT entry_order_type would make a
        # type-based match ambiguous.
        entry, sl_order, tp_order = orders
        side = "buy" if entry.side == OrderSide.BUY else "sell"
        take_profit = TakeProfitRequest(limit_price=str(tp_order.price))
        stop_loss = StopLossRequest(stop_price=str(sl_order.trigger_price))

        try:
            if entry.order_type == OrderType.MARKET:
                request = MarketOrderRequest(
                    symbol=entry.instrument_id.symbol.value,
                    qty=str(entry.quantity),
                    side=side,
                    time_in_force="gtc",
                    order_class="bracket",
                    take_profit=take_profit,
                    stop_loss=stop_loss,
                    client_order_id=str(entry.client_order_id),
                )
            elif entry.order_type == OrderType.LIMIT:
                request = LimitOrderRequest(
                    symbol=entry.instrument_id.symbol.value,
                    qty=str(entry.quantity),
                    side=side,
                    time_in_force="gtc",
                    limit_price=str(entry.price),
                    order_class="bracket",
                    take_profit=take_profit,
                    stop_loss=stop_loss,
                    client_order_id=str(entry.client_order_id),
                )
            else:
                raise ValueError(f"unsupported bracket entry order_type: {entry.order_type}")

            response = await asyncio.to_thread(self._client.submit_order, request)
        except Exception as e:  # noqa: BLE001 - venue rejection must reach the strategy as an event
            reason = f"{type(e).__name__}: {e}"
            for o in orders:
                self.generate_order_rejected(
                    strategy_id=o.strategy_id,
                    instrument_id=o.instrument_id,
                    client_order_id=o.client_order_id,
                    reason=reason,
                    ts_event=self._clock.timestamp_ns(),
                )
            return

        now = self._clock.timestamp_ns()
        self.generate_order_submitted(
            strategy_id=entry.strategy_id, instrument_id=entry.instrument_id,
            client_order_id=entry.client_order_id, ts_event=now,
        )
        self.generate_order_accepted(
            strategy_id=entry.strategy_id, instrument_id=entry.instrument_id,
            client_order_id=entry.client_order_id,
            venue_order_id=VenueOrderId(str(response.id)), ts_event=now,
        )

        # Alpaca returns the SL/TP legs nested under `.legs`, distinguished by
        # order type (a TakeProfitRequest-only leg comes back `type="limit"`, a
        # StopLossRequest-only leg comes back `type="stop"`) rather than by
        # submission position, which Alpaca does not document as stable.
        legs = list(response.legs or [])
        tp_leg = next((leg for leg in legs if str(leg.type.value) == "limit"), None)
        sl_leg = next((leg for leg in legs if str(leg.type.value) in ("stop", "stop_limit")), None)
        if tp_leg is not None:
            self.generate_order_submitted(
                strategy_id=tp_order.strategy_id, instrument_id=tp_order.instrument_id,
                client_order_id=tp_order.client_order_id, ts_event=now,
            )
            self.generate_order_accepted(
                strategy_id=tp_order.strategy_id, instrument_id=tp_order.instrument_id,
                client_order_id=tp_order.client_order_id,
                venue_order_id=VenueOrderId(str(tp_leg.id)), ts_event=now,
            )
        if sl_leg is not None:
            self.generate_order_submitted(
                strategy_id=sl_order.strategy_id, instrument_id=sl_order.instrument_id,
                client_order_id=sl_order.client_order_id, ts_event=now,
            )
            self.generate_order_accepted(
                strategy_id=sl_order.strategy_id, instrument_id=sl_order.instrument_id,
                client_order_id=sl_order.client_order_id,
                venue_order_id=VenueOrderId(str(sl_leg.id)), ts_event=now,
            )

    async def _modify_order(self, command: ModifyOrder) -> None:
        from alpaca.trading.requests import ReplaceOrderRequest

        if command.venue_order_id is None:
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id, instrument_id=command.instrument_id,
                client_order_id=command.client_order_id, venue_order_id=None,
                reason="no venue_order_id on record - cannot replace", ts_event=self._clock.timestamp_ns(),
            )
            return
        request = ReplaceOrderRequest(
            qty=str(command.quantity) if command.quantity is not None else None,
            limit_price=str(command.price) if command.price is not None else None,
            stop_price=str(command.trigger_price) if command.trigger_price is not None else None,
        )
        try:
            await asyncio.to_thread(
                self._client.replace_order_by_id, command.venue_order_id.value, request,
            )
        except Exception as e:  # noqa: BLE001 - a rejected replace must reach the strategy as an event
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id, instrument_id=command.instrument_id,
                client_order_id=command.client_order_id, venue_order_id=command.venue_order_id,
                reason=f"{type(e).__name__}: {e}", ts_event=self._clock.timestamp_ns(),
            )
            return
        # Alpaca's replace confirmation and the resulting OrderUpdated event both
        # arrive over the trade-update WebSocket (`_on_trade_update`, "replaced"/
        # "new"), matching Alpaca's own semantics of a replace as cancel+recreate.

    async def _cancel_order(self, command: CancelOrder) -> None:
        if command.venue_order_id is None:
            self.generate_order_cancel_rejected(
                strategy_id=command.strategy_id, instrument_id=command.instrument_id,
                client_order_id=command.client_order_id, venue_order_id=None,
                reason="no venue_order_id on record - cannot cancel", ts_event=self._clock.timestamp_ns(),
            )
            return
        try:
            await asyncio.to_thread(self._client.cancel_order_by_id, command.venue_order_id.value)
        except Exception as e:  # noqa: BLE001 - a rejected cancel must reach the strategy as an event
            self.generate_order_cancel_rejected(
                strategy_id=command.strategy_id, instrument_id=command.instrument_id,
                client_order_id=command.client_order_id, venue_order_id=command.venue_order_id,
                reason=f"{type(e).__name__}: {e}", ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        try:
            await asyncio.to_thread(self._client.cancel_orders)
        except Exception as e:  # noqa: BLE001 - best-effort, logged not raised (matches on_stop's posture)
            self._log.error(f"cancel_all_orders failed: {type(e).__name__}: {e}")

    async def _batch_cancel_orders(self, command: BatchCancelOrders) -> None:
        for cancel in command.cancels:
            await self._cancel_order(cancel)

    async def _query_account(self, command: QueryAccount) -> None:
        account = await asyncio.to_thread(self._client.get_account)
        self._publish_account_state(account)

    # -- WebSocket trade updates -----------------------------------------------
    async def _on_trade_update(self, update) -> None:
        order = update.order
        client_order_id_str = order.client_order_id
        if not client_order_id_str:
            return  # order this system did not submit (e.g. placed manually)
        client_order_id = ClientOrderId(client_order_id_str)
        strategy_id = self._cache.strategy_id_for_order(client_order_id)
        if strategy_id is None:
            return  # not tracked by this run (e.g. from a prior process instance)

        instrument_id = _instrument_id_for(order.symbol)
        venue_order_id = VenueOrderId(str(order.id))
        now = self._clock.timestamp_ns()
        event = str(update.event.value) if hasattr(update.event, "value") else str(update.event)

        if event in ("fill", "partial_fill"):
            side = OrderSide.BUY if order.side.value == "buy" else OrderSide.SELL
            order_type = _ALPACA_TYPE_TO_NAUTILUS.get(order.type.value, OrderType.MARKET)
            self.generate_order_filled(
                strategy_id=strategy_id, instrument_id=instrument_id,
                client_order_id=client_order_id, venue_order_id=venue_order_id,
                venue_position_id=None, trade_id=TradeId(str(update.execution_id)),
                order_side=side, order_type=order_type,
                last_qty=_qty(update.qty), last_px=_price_or_none(update.price),
                quote_currency=_USD, commission=Money(Decimal(0), _USD),
                liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE, ts_event=now,
            )
        elif event in ("new", "accepted", "pending_new"):
            self.generate_order_accepted(
                strategy_id=strategy_id, instrument_id=instrument_id,
                client_order_id=client_order_id, venue_order_id=venue_order_id, ts_event=now,
            )
        elif event == "canceled":
            self.generate_order_canceled(
                strategy_id=strategy_id, instrument_id=instrument_id,
                client_order_id=client_order_id, venue_order_id=venue_order_id, ts_event=now,
            )
        elif event == "expired":
            self.generate_order_expired(
                strategy_id=strategy_id, instrument_id=instrument_id,
                client_order_id=client_order_id, venue_order_id=venue_order_id, ts_event=now,
            )
        elif event == "rejected":
            self.generate_order_rejected(
                strategy_id=strategy_id, instrument_id=instrument_id,
                client_order_id=client_order_id, reason="rejected by Alpaca", ts_event=now,
            )
        else:
            self._log.debug(f"Unhandled Alpaca trade update event {event!r} for {client_order_id}")

    # -- reconciliation reports --------------------------------------------------
    async def generate_order_status_report(
        self, command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        if command.venue_order_id is not None:
            order = await asyncio.to_thread(
                self._client.get_order_by_id, command.venue_order_id.value,
            )
        elif command.client_order_id is not None:
            order = await asyncio.to_thread(
                self._client.get_order_by_client_id, str(command.client_order_id),
            )
        else:
            raise ValueError("both client_order_id and venue_order_id are None")
        return self._order_to_report(order)

    async def generate_order_status_reports(
        self, command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status="all" if not command.open_only else "open", limit=500)
        orders = await asyncio.to_thread(self._client.get_orders, request)
        reports = []
        for order in orders:
            if order.client_order_id is None:
                continue
            if command.instrument_id is not None and order.symbol != command.instrument_id.symbol.value:
                continue
            reports.append(self._order_to_report(order))
        self._log_report_receipt(len(reports), "OrderStatusReport", self._log.info)
        return reports

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status="closed", limit=500)
        orders = await asyncio.to_thread(self._client.get_orders, request)
        reports = []
        for order in orders:
            if order.client_order_id is None or order.filled_qty in (None, "0"):
                continue
            if command.instrument_id is not None and order.symbol != command.instrument_id.symbol.value:
                continue
            side = OrderSide.BUY if order.side.value == "buy" else OrderSide.SELL
            reports.append(
                FillReport(
                    account_id=self.account_id,
                    instrument_id=_instrument_id_for(order.symbol),
                    venue_order_id=VenueOrderId(str(order.id)),
                    trade_id=TradeId(str(order.id)),
                    order_side=side,
                    last_qty=_qty(order.filled_qty),
                    last_px=_price_or_none(order.filled_avg_price) or Price.from_str("0"),
                    commission=Money(Decimal(0), _USD),
                    liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
                    report_id=UUID4(),
                    ts_event=self._clock.timestamp_ns(),
                    ts_init=self._clock.timestamp_ns(),
                    client_order_id=ClientOrderId(order.client_order_id),
                ),
            )
        self._log_report_receipt(len(reports), "FillReport", self._log.info)
        return reports

    async def generate_position_status_reports(
        self, command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        positions = await asyncio.to_thread(self._client.get_all_positions)
        reports = []
        for position in positions:
            if command.instrument_id is not None and position.symbol != command.instrument_id.symbol.value:
                continue
            side = PositionSide.LONG if position.side.value == "long" else PositionSide.SHORT
            reports.append(
                PositionStatusReport(
                    account_id=self.account_id,
                    instrument_id=_instrument_id_for(position.symbol),
                    position_side=side,
                    quantity=Quantity.from_str(str(abs(Decimal(str(position.qty))))),
                    report_id=UUID4(),
                    ts_last=self._clock.timestamp_ns(),
                    ts_init=self._clock.timestamp_ns(),
                    avg_px_open=Decimal(str(position.avg_entry_price)),
                ),
            )
        self._log_report_receipt(len(reports), "PositionStatusReport", self._log.info)
        return reports

    def _order_to_report(self, order) -> OrderStatusReport:
        status = _ALPACA_STATUS_TO_NAUTILUS.get(order.status.value, NautilusOrderStatus.ACCEPTED)
        order_type = _ALPACA_TYPE_TO_NAUTILUS.get(order.type.value, OrderType.MARKET)
        side = OrderSide.BUY if order.side.value == "buy" else OrderSide.SELL
        tif = TimeInForce.GTC if order.time_in_force.value == "gtc" else TimeInForce.DAY
        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=_instrument_id_for(order.symbol),
            venue_order_id=VenueOrderId(str(order.id)),
            order_side=side,
            order_type=order_type,
            time_in_force=tif,
            order_status=status,
            quantity=_qty(order.qty or "0"),
            filled_qty=_qty(order.filled_qty or "0"),
            report_id=UUID4(),
            ts_accepted=self._clock.timestamp_ns(),
            ts_last=self._clock.timestamp_ns(),
            ts_init=self._clock.timestamp_ns(),
            client_order_id=(
                ClientOrderId(order.client_order_id) if order.client_order_id else None
            ),
            price=_price_or_none(order.limit_price),
            trigger_price=_price_or_none(order.stop_price),
            avg_px=Decimal(str(order.filled_avg_price)) if order.filled_avg_price else None,
        )
