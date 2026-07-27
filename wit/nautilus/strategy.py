"""``WitStrategy`` — one instance per instrument, the build plan's
`Orchestrator.process_symbol` sequence re-expressed as `on_start`/`on_bar`/
`_on_decision`/`on_order_filled` (build plan §3 Phase N5).

Two-path design forced by the event-loop constraint (build plan §1.2): the
committee is 3 rate-limited LLM calls, up to ~90s each — `on_bar` must return
in microseconds, so it does only the synchronous, cheap work (desks, gates,
report), then hands off to `self.run_in_executor(self._on_decision, ...)`.
Confirmed in Phase N0 and re-verified against the installed
`nautilus_trader==1.230.0` before writing this file:
`run_in_executor` dispatches to a registered thread-pool executor in live
mode and calls the function immediately/synchronously in backtest — so
`DecisionProvider.decide()` staying a plain synchronous method (see
`wit/committee/provider.py`) is exactly what this hand-off needs, no
`asyncio` involved on either side.

Fund-wide state — kill switch, adaptive-sizing multipliers, the dream state —
lives in `FundStateActor` (`wit/nautilus/actor.py`), shared by direct Python
reference (not Nautilus config, which must stay serializable) since both are
constructed by the same assembly code before being added to the `Trader`.
Cross-instrument state that MT5's `Orchestrator` had to track by hand — open
positions fund-wide, per-symbol, and by correlation group — is read straight
from `self.cache.positions_open()`, which Nautilus already shares across
every strategy in the `Trader` for free.

Exit-aware cooldown for free: `on_position_closed` gives a real broker-side
exit timestamp, which the MT5 build never had (its cooldown was entry-based
because MT5 exits are broker-side and invisible to its journal — see
`wit/ops/journal.py`'s module docstring).
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Currency
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from wit.committee.contract import CommitteeDecision
from wit.committee.provider import DecisionProvider
from wit.config import CONFIG
from wit.desks import garch, market_intel, markov, quant_analyst, technicals
from wit.desks.quant_analyst import QuantAnalystReport
from wit.nautilus.actor import FundStateActor
from wit.ops import market_hours, prefilter
from wit.ops.journal import Journal
from wit.risk.account import AccountSnapshot
from wit.risk.instrument_spec import InstrumentSpec, spec_for
from wit.risk.sizing import TradePlan, build_plan, revalidate_plan


class WitStrategyConfig(StrategyConfig, frozen=True):
    """Configuration for a ``WitStrategy`` instance.

    ``symbol`` is the logical, MT5-style symbol the desks/committee/risk
    layers key everything by (e.g. ``"NVDA"``, ``"EURUSD"``) — deliberately
    decoupled from ``instrument_id``'s Nautilus/IB string form (e.g.
    ``"EUR/USD.IDEALPRO"``), since desk/risk code was ported keyed by the
    former and must not be made to parse the latter.
    """

    instrument_id: InstrumentId
    bar_type: BarType
    symbol: str
    timeframe: str = "H1"
    history_bars: int = 750
    account_currency: str = "USD"
    value_per_unit: float = 1.0
    min_stop_distance: float = 0.0
    # Off by default (Phase N7): market_intel.compute() makes a live yfinance/
    # Finnhub HTTP call, which a backtest must never depend on for
    # determinism or for running offline in CI - the MT5 build had the same
    # split (engine/orchestrator.py's live cycle calls it; engine/backtest.py
    # never imports it at all). node_live.py's build_strategies() turns this
    # on explicitly for live/paper.
    enable_market_intel: bool = False
    # The venue the ACCOUNT is registered under - not necessarily the same as
    # instrument_id.venue. Defaults to None, resolved at lookup time to
    # instrument_id.venue (Phase N5 backtest's single-venue setup, unchanged).
    # A multi-venue broker like IB registers the account under its own fixed
    # pseudo-venue (see wit/nautilus/node_live.py's IB_VENUE), separate from
    # any instrument's SMART/NASDAQ/IDEALPRO routing venue - passing that
    # through here is what Phase N6 audit finding F4 requires: without it,
    # every decision dies at "no_account_snapshot" because the account is
    # never found under an exchange-routing venue.
    account_venue: Venue | None = None


def _bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    """Nautilus `Bar` objects -> the OHLCV DataFrame shape the desks expect
    (``open``/``high``/``low``/``close``/``tick_volume``, ascending UTC time
    index). Sorted explicitly by ``ts_event`` rather than trusting
    ``Cache.bars()``'s return order, since that isn't documented and this
    function has no kernel to inspect it against in a unit test."""
    rows = sorted(bars, key=lambda b: b.ts_event)
    return pd.DataFrame(
        {
            "open": [float(b.open) for b in rows],
            "high": [float(b.high) for b in rows],
            "low": [float(b.low) for b in rows],
            "close": [float(b.close) for b in rows],
            "tick_volume": [float(b.volume) for b in rows],
        },
        index=pd.DatetimeIndex([unix_nanos_to_dt(b.ts_event) for b in rows], tz="UTC"),
    )


class WitStrategy(Strategy):
    """Implements the build plan's per-instrument decision sequence.
    ``provider``/``fund_state`` are plain Python references (not part of the
    serializable ``config``), wired by whatever assembly code constructs the
    `Trader` (Phase N6's `node_live.py` / `node_backtest.py`)."""

    def __init__(
        self,
        config: WitStrategyConfig,
        provider: DecisionProvider,
        fund_state: FundStateActor,
        journal: Journal | None = None,
    ) -> None:
        super().__init__(config)
        self.provider = provider
        self.fund_state = fund_state
        self.journal = journal or Journal(CONFIG.journal_path)
        self.instrument = None
        self.spec: InstrumentSpec | None = None
        self._account_currency = Currency.from_str(config.account_currency)

    # -- lifecycle ---------------------------------------------------------
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {self.config.instrument_id}")
            self.stop()
            return
        self.spec = spec_for(
            self.instrument,
            value_per_unit=self.config.value_per_unit,
            min_stop_distance=self.config.min_stop_distance,
        )

        self.request_bars(
            self.config.bar_type,
            start=self.clock.utc_now() - timedelta(hours=self.config.history_bars * 2),
            limit=self.config.history_bars,
            callback=lambda _: self._on_warmup_complete(),
        )

    def _on_warmup_complete(self) -> None:
        self.subscribe_bars(self.config.bar_type)
        self.subscribe_quote_ticks(self.config.instrument_id)
        self.log.info(
            f"{self.config.symbol}: warmup complete "
            f"({self.cache.bar_count(self.config.bar_type)} bars), subscribed to live data"
        )

    def on_stop(self) -> None:
        # Cancel working orders but do not close positions - same posture as
        # the MT5 build (build plan Phase N5).
        self.cancel_all_orders(self.config.instrument_id)
        self.unsubscribe_bars(self.config.bar_type)
        self.unsubscribe_quote_ticks(self.config.instrument_id)

    # -- fast path: hands off immediately, does no work on the loop ---------
    def on_bar(self, bar: Bar) -> None:
        """Schedules everything else off-loop. Originally this method also ran
        the desk computation (technicals/markov/garch) inline before handing
        off to `_on_decision` for the committee call — the docstring's
        "must return in microseconds" claim was false while it did that:
        `garch.compute` alone measured ~1s cold (a real scipy optimization)
        directly on the event loop (Phase N5 audit finding F7). Since
        `run_in_executor` degrades to a synchronous inline call in backtest
        (same total work, same order — this is not a behavior change there)
        and dispatches to a worker thread in live, moving the whole body
        behind that boundary fixes the live-mode violation for free."""
        if self.instrument is None or self.spec is None:
            return
        self.run_in_executor(self._on_bar_work, args=(bar,))

    def _on_bar_work(self, bar: Bar) -> None:
        symbol = self.config.symbol

        bars = self.cache.bars(self.config.bar_type)
        if len(bars) < 101:  # technicals needs >= 100; markov/garch need it too
            return
        frame = _bars_to_frame(bars)

        tech = technicals.compute(symbol, frame)
        mk = markov.compute(symbol, frame)
        gk = garch.compute(symbol, frame, self.config.timeframe)

        tradeable, closed_reason = market_hours.is_tradeable(symbol, CONFIG.session)
        if not tradeable:
            self._journal_synthetic(
                symbol, bar,
                CommitteeDecision(
                    symbol=symbol, action="HOLD", conviction=0.0, risk_rating="low",
                    rationale=f"Market closed: {closed_reason}. Committee not convened.",
                    key_risk="none - instrument's market is not open",
                    stop_atr_mult=2.0, reward_risk=1.5, model="market_hours",
                    detail={"market_closed": True, "reason": closed_reason},
                ),
                tech, mk, gk,
            )
            return

        skip, reason = prefilter.should_skip(tech, mk, CONFIG.prefilter)
        if skip:
            self._journal_synthetic(symbol, bar, prefilter.synthetic_hold(symbol, reason),
                                    tech, mk, gk)
            return

        intel = market_intel.compute(symbol, CONFIG.intel) if self.config.enable_market_intel else None
        report = quant_analyst.compute(
            symbol, self.config.timeframe, tech, mk, gk,
            intel=intel, dream=self.fund_state.dream_state,
        )
        # Already off-loop (or, in backtest, executing synchronously anyway) -
        # continue straight into the deliberation callback rather than
        # scheduling a second executor hop.
        self._on_decision(report, bar.ts_event)

    def _journal_synthetic(
        self, symbol: str, bar: Bar, decision: CommitteeDecision,
        tech, mk, gk,
    ) -> None:
        """A gate fired before the committee would have been convened - no
        LLM spend, so no off-loop hop needed. Still journalled like any other
        decision (build plan: "a gated HOLD is auditable like any other
        decision")."""
        report = quant_analyst.compute(symbol, self.config.timeframe, tech, mk, gk,
                                       dream=self.fund_state.dream_state)
        plan = TradePlan(symbol=symbol, approved=False, action="HOLD",
                         blocked_by=[decision.rationale])
        self.journal.log_decision(symbol, decision, plan, report)

    # -- deliberation callback: runs off-loop, safe to block -----------------
    def _on_decision(self, report: QuantAnalystReport, bar_ts_ns: int) -> None:
        symbol = self.config.symbol
        if self.fund_state.is_halted():
            self.journal.log_event("halted", self.fund_state.halt_reason or "halted",
                                   symbol=symbol)
            return

        decision = self.provider.decide(
            report, instrument_id=str(self.config.instrument_id), bar_ts_ns=bar_ts_ns
        )

        account = self._account_snapshot()
        if account is None:
            self.journal.log_event("no_account_snapshot",
                                   "portfolio/account data unavailable", symbol=symbol)
            return

        quote = self.cache.quote_tick(self.config.instrument_id)
        spread = float(quote.ask_price) - float(quote.bid_price) if quote is not None else -1.0

        open_positions = self.cache.positions_open()
        open_positions_symbol = self.cache.positions_open(instrument_id=self.config.instrument_id)
        open_symbols = tuple(p.instrument_id.symbol.value for p in open_positions)

        kelly_mult, drawdown_mult = self.fund_state.size_multipliers()

        plan = build_plan(
            decision=decision, tech=report.technicals, mk=report.markov, gk=report.garch,
            account=account, spec=self.spec, spread=spread,
            open_positions_total=len(open_positions),
            open_positions_symbol=len(open_positions_symbol),
            open_symbols=open_symbols,
            in_cooldown=self._in_cooldown(),
            margin_fn=None,  # margin_fn wiring lands with N6's IB execution client
            kelly_mult=kelly_mult, drawdown_mult=drawdown_mult,
        )

        order_result = None
        if plan.approved:
            if quote is None:
                self.journal.log_event("no_quote_at_execution", "no live quote available",
                                       symbol=symbol)
            else:
                reject = revalidate_plan(plan, float(quote.bid_price), float(quote.ask_price),
                                         self.spec, spread)
                if reject:
                    self.journal.log_event("revalidation_block", reject, symbol=symbol)
                # Re-check right before submitting, not just at the top of this
                # method (Phase N5 audit finding F2): provider.decide() can run
                # up to ~90s per call, and this whole method runs off-loop via
                # run_in_executor - an operator's `on_stop`/kill-switch signal
                # during that window must not be followed by a submit anyway.
                # Without this, on_stop's cancel_all_orders can run BEFORE this
                # order even exists, leaving an unmanaged live position the
                # strategy believes it is flat on.
                elif not self.is_running:
                    self.journal.log_event("stopped_before_submit",
                                           "strategy stopped while deliberating", symbol=symbol)
                elif self.fund_state.is_halted():
                    self.journal.log_event("halted_before_submit",
                                           self.fund_state.halt_reason or "halted", symbol=symbol)
                else:
                    order_result = self._submit(plan)

        # Phase N5 audit finding F8: client_order_id was computed in
        # _submit's return value but never threaded into log_decision's own
        # client_order_id field (only into the nested `order` dict) - every
        # decision record's top-level identifier stayed blank.
        client_order_id = (order_result or {}).get("client_order_id", "")
        self.journal.log_decision(symbol, decision, plan, report, order_result,
                                  client_order_id=client_order_id)

    def _in_cooldown(self) -> bool:
        minutes = CONFIG.risk.cooldown_minutes
        if minutes <= 0:
            return False
        closed = self.cache.positions_closed(instrument_id=self.config.instrument_id)
        if not closed:
            return False
        last_close_ns = max(p.ts_closed for p in closed if p.ts_closed is not None)
        last_close = unix_nanos_to_dt(last_close_ns)
        # self.clock.utc_now(), not datetime.now(UTC) (Phase N5 audit finding
        # F3): ts_closed is simulated time in a backtest, potentially months
        # away from wall-clock "now" - comparing against the real system clock
        # made this gate permanently inert in backtest (elapsed always huge),
        # silently overstating backtest performance by permitting re-entries
        # live trading would refuse.
        elapsed = (self.clock.utc_now() - last_close).total_seconds() / 60.0
        return elapsed < minutes

    def _account_snapshot(self) -> AccountSnapshot | None:
        # account_venue, not instrument_id.venue (Phase N6 audit finding F4):
        # a multi-venue broker (IB) registers the account under its own fixed
        # venue, never under an instrument's exchange-routing venue - using
        # the latter meant this always returned None against IB, and every
        # decision died here before build_plan was ever reached.
        venue = self.config.account_venue or self.config.instrument_id.venue
        equity_by_ccy = self.portfolio.equity(venue)
        account = self.portfolio.account(venue)
        if not equity_by_ccy or account is None:
            return None
        equity_money = equity_by_ccy.get(self._account_currency)
        margin_free_money = account.balance_free(self._account_currency)
        if equity_money is None or margin_free_money is None:
            return None
        return AccountSnapshot(equity=float(equity_money), margin_free=float(margin_free_money))

    def _submit(self, plan: TradePlan) -> dict:
        side = OrderSide.BUY if plan.action == "BUY" else OrderSide.SELL
        order_list = self.order_factory.bracket(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=self.instrument.make_qty(plan.quantity),
            entry_price=self.instrument.make_price(plan.entry),
            sl_trigger_price=self.instrument.make_price(plan.stop_loss),
            tp_price=self.instrument.make_price(plan.take_profit),
        )
        self.submit_order_list(order_list)
        return {"ok": True, "client_order_id": str(order_list.orders[0].client_order_id)}

    # -- fill/exit journalling (exit-aware cooldown, for free) ---------------
    def on_order_filled(self, event) -> None:
        self.journal.log_event(
            "order_filled", f"{event.order_side} {event.last_qty} @ {event.last_px}",
            symbol=self.config.symbol, client_order_id=str(event.client_order_id),
            position_id=str(event.position_id) if event.position_id else "",
        )

    def on_position_closed(self, event) -> None:
        # realized_pnl is now a structured field, not just embedded in the
        # message string (Phase N7 audit finding C1/M3): Reflection.review()
        # reads it directly, since under OmsType.NETTING - the only OMS this
        # system runs (the IBKR exec client hard-codes it) - position_id is
        # a constant per (instrument, strategy), not a trade identifier, and
        # Cache.positions_closed() evicts a symbol's prior closed position
        # the moment it's re-entered. This event, journalled once per real
        # round trip as it happens, is the only complete record.
        realized_pnl = float(event.realized_pnl)
        self.journal.log_event(
            "position_closed", f"realized_pnl={realized_pnl}",
            symbol=self.config.symbol, position_id=str(event.position_id),
            realized_pnl=realized_pnl,
        )
        # Same reasoning feeds the daily-loss breaker and Kelly sizing
        # directly (Phase N7 audit finding C2/H2): FundStateActor's own
        # accumulator, not a cache query that the same NETTING eviction
        # would starve of history.
        self.fund_state.record_realized_pnl(realized_pnl, event.ts_closed)
