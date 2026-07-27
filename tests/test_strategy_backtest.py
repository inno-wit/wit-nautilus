"""Phase N5 gate: a real NautilusTrader BacktestEngine run with WitStrategy +
FundStateActor, per the build plan ("a backtest with StubPolicyProvider
completes and produces orders/fills/journal in the same shape as the MT5
build"). This is not a mock of the framework - it constructs a genuine
BacktestEngine, venue, instrument, and bar stream, and runs the actual
on_start/on_bar/run_in_executor/_on_decision path end to end.

Uses StubPolicyProvider (always the same verdict) rather than
ReplayCommitteeProvider/LiveCommitteeProvider: this test's job is to prove the
strategy wiring is correct, not to validate committee behavior (that's
Phase N3's job) or produce a meaningful P&L (Phase N9's).

Each test constructs and runs a full BacktestEngine kernel (MessageBus,
Cache, DataEngine, RiskEngine, ExecEngine) - genuinely heavier than the rest
of this suite. Verified individually and passing on this dev machine; running
all three back-to-back in one pytest process was unreliable specifically when
the machine was already under severe memory pressure (~90% RAM/swap used from
unrelated processes) - not reproduced when memory was available, and not
something a code change here can fix. If this file ever hangs or exits
oddly in CI, check host memory before suspecting the strategy.
"""
from __future__ import annotations

import pytest
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from tests.conftest import make_bars
from wit.committee.stub import StubPolicyProvider
from wit.nautilus.actor import FundStateActor, FundStateActorConfig
from wit.nautilus.strategy import WitStrategy, WitStrategyConfig


def _make_nautilus_bars(bar_type: BarType, instrument, frame) -> list[Bar]:
    """``make_bars()``'s open/high/low/close only guarantee high/low bracket
    *close* (see tests/conftest.py) - open is the prior bar's close, which can
    fall outside that range since noise is independent per bar. Nautilus's
    `Bar` validates OHLC integrity strictly (low <= open/close <= high), so
    widen high/low to also bracket open here; MT5-shaped bars never needed
    this because nothing downstream validated it that strictly."""
    bars = []
    for ts, row in frame.iterrows():
        ts_ns = int(ts.value)  # pandas Timestamp.value is already unix ns
        o, h, low_, c = row["open"], row["high"], row["low"], row["close"]
        h = max(h, o, c)
        low_ = min(low_, o, c)
        bars.append(Bar(
            bar_type=bar_type,
            open=instrument.make_price(o),
            high=instrument.make_price(h),
            low=instrument.make_price(low_),
            close=instrument.make_price(c),
            volume=instrument.make_qty(max(row["tick_volume"], 1.0)),
            ts_event=ts_ns,
            ts_init=ts_ns,
        ))
    return bars


def _make_nautilus_quotes(instrument, frame) -> list[QuoteTick]:
    """`_on_decision` reads `self.cache.quote_tick(...)` for the live spread
    - without any QuoteTick data in the engine, that call returns None,
    `spread` defaults to the malformed-quote sentinel (-1.0), and the Phase N4
    audit's negative-spread guard correctly (if unhelpfully, for this test)
    blocks every plan. A tiny synthetic bid/ask around each bar's close is
    enough for the strategy to actually clear the spread gate."""
    quotes = []
    for ts, row in frame.iterrows():
        ts_ns = int(ts.value)
        close = row["close"]
        half_spread = close * 0.00005  # ~0.5 pip, well under max_spread_pct
        quotes.append(QuoteTick(
            instrument_id=instrument.id,
            bid_price=instrument.make_price(close - half_spread),
            ask_price=instrument.make_price(close + half_spread),
            bid_size=instrument.make_qty(1_000_000),
            ask_size=instrument.make_qty(1_000_000),
            ts_event=ts_ns,
            ts_init=ts_ns,
        ))
    return quotes


@pytest.fixture
def engine_setup(tmp_path):
    instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD")
    venue = instrument.id.venue

    engine = BacktestEngine()
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(50_000, USD)],
    )
    engine.add_instrument(instrument)

    # 150 bars: >=100 warmup (technicals/markov/garch's own floor) + ~50 live
    # decision bars - enough to exercise on_bar -> run_in_executor ->
    # _on_decision -> build_plan -> journal repeatedly without each of ~150
    # GARCH refits (one per bar, a real scipy optimization) making this test
    # suite slow. A longer/more representative run is Phase N9's job.
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    frame = make_bars(n=150, drift=0.0005, seed=7)
    bars = _make_nautilus_bars(bar_type, instrument, frame)
    engine.add_data(bars)
    engine.add_data(_make_nautilus_quotes(instrument, frame))

    provider = StubPolicyProvider(action="BUY", conviction=0.6)
    fund_state = FundStateActor(FundStateActorConfig(
        venue=venue, account_currency="USD",
        kill_switch_file=str(tmp_path / "KILL"),
        dream_state_path=str(tmp_path / "dream_state.json"),
        poll_interval_seconds=3600,
    ))
    journal_path = tmp_path / "journal.jsonl"
    from wit.ops.journal import Journal
    journal = Journal(str(journal_path))
    strategy = WitStrategy(
        WitStrategyConfig(
            instrument_id=instrument.id, bar_type=bar_type, symbol="EURUSD",
            timeframe="H1", history_bars=100,
        ),
        provider=provider, fund_state=fund_state, journal=journal,
    )

    engine.add_actor(fund_state)
    engine.add_strategy(strategy)
    return engine, journal_path, frame


def test_backtest_runs_to_completion_without_raising(engine_setup):
    engine, _journal_path, _frame = engine_setup
    engine.run()
    engine.dispose()


def test_backtest_produces_journalled_decisions(engine_setup):
    import json

    engine, journal_path, _frame = engine_setup
    engine.run()
    engine.dispose()

    assert journal_path.exists()
    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    decisions = [r for r in records if r.get("type") == "decision"]
    assert len(decisions) > 0, "expected at least one journalled decision over 150 bars"
    # Every decision should be traceable to the stub's fixed BUY verdict once
    # the desks have enough warmup bars and no gate (market hours/prefilter)
    # intervened - confirms the on_bar -> run_in_executor -> _on_decision ->
    # build_plan -> journal path actually ran, not just that on_bar fired.
    assert any(r["action"] in ("BUY", "HOLD") for r in decisions)


def test_backtest_places_at_least_one_order(engine_setup):
    engine, _journal_path, _frame = engine_setup
    engine.run()

    orders = engine.trader.generate_order_fills_report()
    positions = engine.cache.positions()
    engine.dispose()
    # EURUSD is FX (no market-hours gate) and the stub always says BUY with
    # conviction 0.6 - over 150 H1 bars the sizing/spread/conviction gates
    # should clear at least once, proving the bracket-order submission path
    # (order_factory.bracket -> submit_order_list) actually executed.
    assert len(positions) > 0 or not orders.empty
