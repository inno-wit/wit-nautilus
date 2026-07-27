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
of this suite. Running more than one bare BacktestEngine in a single process
previously crashed the interpreter (Windows STATUS_STACK_BUFFER_OVERRUN) after
the first `dispose()`. Phase N5's audit traced the real cause: nautilus_trader's
Rust logger is a process-global singleton, and the second engine's init panics
trying to install it again - reproduced with zero project code (three bare
`BacktestEngine()`s), nothing to do with host memory (an earlier note here
wrongly blamed memory pressure). `bypass_logging=True` below sidesteps the
second init entirely and all three tests pass together reliably.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common.config import LoggingConfig
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

    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True))
    )
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
    # Phase N5 audit finding F13: the original assertion here
    # (any(action in ("BUY", "HOLD"))) is tautological - build_plan forces
    # action to "HOLD" on any block and the stub only ever proposes "BUY", so
    # no reachable state could fail it. Require what the stub actually
    # requests to reach the strategy at all - if every gate silently blocked
    # every bar, this fails where the old assertion couldn't.
    assert any(r["action"] == "BUY" for r in decisions), (
        "expected at least one BUY-proposed decision - "
        f"got actions: {sorted({r['action'] for r in decisions})}"
    )


def test_journal_entries_are_stamped_with_simulated_time_not_wall_clock(engine_setup):
    """Phase N7 audit finding, round 8: Journal.write's ts field used to
    default to real wall-clock datetime.now(UTC) regardless of caller
    context, so a backtest's journal entries were all stamped with
    whatever real date the test happened to run on - not the 2026-01-01+
    simulated dates the bars themselves occupy. Reflection's "last N days"
    windowing only means anything if entries carry the same clock its
    caller does. Journal.log_decision/log_event's ts= parameter, threaded
    from self.clock.utc_now() throughout WitStrategy, fixes this."""
    import json

    engine, journal_path, _frame = engine_setup
    engine.run()
    engine.dispose()

    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    decisions = [r for r in records if r.get("type") == "decision"]
    assert decisions, "expected at least one journalled decision"
    for r in decisions:
        ts = datetime.fromisoformat(r["ts"])
        assert ts.year == 2026 and ts.month == 1, (
            f"decision ts={r['ts']!r} is not in the simulated backtest period "
            "(2026-01) - looks like wall-clock time leaked into the journal"
        )


def test_backtest_places_at_least_one_order(engine_setup):
    engine, _journal_path, _frame = engine_setup
    engine.run()

    # Phase N5 audit finding F13: check the order cache directly rather than
    # an `or` across two proxies (positions / a fills report) that couldn't
    # distinguish "an order was placed" from "a position exists" - EURUSD is
    # FX (no market-hours gate) and the stub always says BUY with conviction
    # 0.6, so over 150 H1 bars the sizing/spread/conviction gates should clear
    # at least once, proving order_factory.bracket -> submit_order_list
    # actually executed.
    orders = engine.cache.orders()
    engine.dispose()
    assert len(orders) > 0, "expected at least one order submitted over 150 bars"


def test_fund_state_day_rollover_tracks_simulated_time_not_wall_clock(engine_setup):
    """Phase N5 audit finding F1: FundStateActor._recompute_multipliers used
    to read datetime.now(UTC) - wall-clock time - for day rollover, while
    every bar/position timestamp it's compared against is simulated time.
    make_bars()'s 150 H1 bars start 2026-01-01 (see tests/conftest.py) and
    span ~6 days; if the actor's day-tracking is on wall-clock time instead of
    self.clock.utc_now(), it either never rolls over (frozen at whatever wall
    date the test happened to run on) or - worse - "rolls over" using a wall
    date nowhere near the bars being processed. Either way it won't land in
    January 2026, which is what a correct simulated-clock read must produce."""
    engine, _journal_path, _frame = engine_setup
    fund_state = engine.trader.actors()[0]
    assert fund_state.__class__.__name__ == "FundStateActor"

    engine.run()
    final_day = fund_state._day
    engine.dispose()

    assert final_day is not None
    assert final_day.year == 2026 and final_day.month == 1, (
        f"expected the actor's tracked day to land in Jan 2026 (simulated time "
        f"the bars actually occupy), got {final_day} - looks like wall-clock time leaked in"
    )


# ── Phase N7: daily/weekly cron-equivalents fire on simulated time ─────────

class _FakeDreamCommittee:
    """A dream-capable committee stand-in - LiveCommitteeProvider needs a
    real ANTHROPIC_API_KEY, which this test suite must not depend on."""

    def __init__(self):
        self.calls = 0

    def dream(self, qualifying, scores, window_days, min_bucket_trades):
        self.calls += 1
        return []


def test_daily_and_weekly_timers_fire_during_a_multi_day_backtest(tmp_path, capsys):
    """make_bars()'s 150 H1 bars start 2026-01-01 (Thursday) and span ~6
    days, crossing Sunday 2026-01-04 - long enough for every one of the
    three Phase N7 cron-equivalents (daily briefing 00:05 UTC, daily review
    23:55 UTC, weekly dream Sunday 22:30 UTC) to fire at least once on
    simulated time. Wires a real Journal and a fake dream-capable committee
    (not LiveCommitteeProvider, which needs a real API key) and asserts on
    what actually happened - not just that nothing raised."""
    instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD")
    venue = instrument.id.venue

    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
        base_currency=USD, starting_balances=[Money(50_000, USD)],
    )
    engine.add_instrument(instrument)

    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    frame = make_bars(n=150, drift=0.0005, seed=7)
    engine.add_data(_make_nautilus_bars(bar_type, instrument, frame))
    engine.add_data(_make_nautilus_quotes(instrument, frame))

    from wit.ops.journal import Journal
    journal_path = tmp_path / "journal.jsonl"
    journal = Journal(str(journal_path))
    committee = _FakeDreamCommittee()
    dream_state_path = tmp_path / "dream_state.json"
    fund_state = FundStateActor(
        FundStateActorConfig(
            venue=venue, account_currency="USD",
            kill_switch_file=str(tmp_path / "KILL"),
            dream_state_path=str(dream_state_path),
            poll_interval_seconds=3600,
        ),
        journal=journal, committee=committee,
    )
    strategy = WitStrategy(
        WitStrategyConfig(instrument_id=instrument.id, bar_type=bar_type, symbol="EURUSD",
                          timeframe="H1", history_bars=100),
        provider=StubPolicyProvider(action="BUY", conviction=0.6),
        fund_state=fund_state, journal=journal,
    )
    engine.add_actor(fund_state)
    engine.add_strategy(strategy)

    from wit.config import CONFIG
    production_path = Path(CONFIG.dream.state_path)
    production_mtime_before = production_path.stat().st_mtime if production_path.exists() else None

    engine.run()
    engine.dispose()

    # dream.run() calls save() unconditionally, even with zero qualifying
    # buckets (no positions closed within a 6-day run is expected - a
    # bracket order's SL/TP may not trigger that fast) - so this is the
    # reliable signal the weekly timer actually fired, independent of
    # whether the committee itself got called.
    assert dream_state_path.exists(), "dream.run() never saved state - the weekly timer didn't fire"

    # Regression guard, same reasoning as kill_switch_file having no
    # fallback default (Phase N5 audit finding F4): _on_weekly_dream must
    # save to the actor's OWN configured dream_state_path, never silently
    # fall through to the real production data/dream_state.json just
    # because it read CONFIG.dream directly.
    assert not production_path.exists() or production_path.stat().st_mtime == production_mtime_before, (
        "the weekly dream timer wrote to the production dream_state.json "
        "instead of the actor's own configured dream_state_path"
    )

    kinds = [r.get("kind") for r in journal.read() if r.get("type") == "event"]
    assert "dream_cycle" in kinds
    assert "review_error" not in kinds, "daily review raised - see the journal for the traceback"
    assert "briefing_error" not in kinds, "daily briefing raised - see the journal for the traceback"
    assert "dream_error" not in kinds, "weekly dream raised - see the journal for the traceback"

    # Positive signal, not just error-absence (Phase N7 audit finding I1):
    # briefing/review only print (via Alerter.send) rather than journalling
    # on success, so "no *_error kind" alone can't distinguish "fired
    # cleanly" from "never fired at all". Both must actually have printed
    # at least once over six simulated days.
    out = capsys.readouterr().out
    assert out.count("Daily briefing") >= 1, "daily briefing timer never printed its output"
    assert out.count("Reflection") >= 1, "daily review timer never printed its output"


# ── Phase N7 audit findings C1/C2/H2: NETTING re-entry evicts a symbol's
# prior closed position from Cache - proven here against a real multi-trade
# backtest, the same way the bugs themselves were originally caught. ────────

def _run_multi_trade_backtest(tmp_path, *, kill_switch_file=None, actor_kwargs=None):
    """Shared setup: 400 H1 bars (long enough for several real round trips -
    the 150-bar fixture other tests use rarely closes more than one or two),
    StubPolicyProvider always BUY, a real FundStateActor + WitStrategy pair.
    Returns (fund_state, journal, closed_pnls_from_journal)."""
    instrument = TestInstrumentProvider.default_fx_ccy("EUR/USD")
    venue = instrument.id.venue
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
        base_currency=USD, starting_balances=[Money(50_000, USD)],
    )
    engine.add_instrument(instrument)
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    frame = make_bars(n=400, drift=0.0005, seed=7)
    engine.add_data(_make_nautilus_bars(bar_type, instrument, frame))
    engine.add_data(_make_nautilus_quotes(instrument, frame))

    from wit.ops.journal import Journal
    journal = Journal(str(tmp_path / "journal.jsonl"))
    fund_state = FundStateActor(
        FundStateActorConfig(
            venue=venue, account_currency="USD",
            kill_switch_file=str(kill_switch_file or tmp_path / "KILL"),
            dream_state_path=str(tmp_path / "dream_state.json"),
            poll_interval_seconds=3600,
        ),
        journal=journal, **(actor_kwargs or {}),
    )
    strategy = WitStrategy(
        WitStrategyConfig(instrument_id=instrument.id, bar_type=bar_type, symbol="EURUSD",
                          timeframe="H1", history_bars=100),
        provider=StubPolicyProvider(action="BUY", conviction=0.6),
        fund_state=fund_state, journal=journal,
    )
    engine.add_actor(fund_state)
    engine.add_strategy(strategy)
    engine.run()
    engine.dispose()

    import json
    journal_pnls = [
        r["realized_pnl"] for r in
        (json.loads(line) for line in Path(journal.path).read_text(encoding="utf-8").splitlines()
         if line.strip())
        if r.get("kind") == "position_closed"
    ]
    return fund_state, journal, journal_pnls


def test_realized_pnl_since_sums_every_closed_trade_not_just_the_last(tmp_path):
    """Phase N7 audit finding C2: under OmsType.NETTING, Cache holds at most
    one closed position per instrument - _realized_pnl_since used to read
    0.0 after any number of real closed trades on one symbol. Proven here by
    running a real multi-trade backtest and checking the actor's own
    accumulator against the journal's independent record of every close."""
    fund_state, _journal, journal_pnls = _run_multi_trade_backtest(tmp_path)

    assert len(journal_pnls) >= 3, (
        f"need several real closed trades to prove summation, got {len(journal_pnls)} - "
        "widen the bar window if this becomes flaky"
    )
    assert fund_state._realized_pnl_since(0) == sum(journal_pnls)


def test_closed_pnls_rehydrate_from_the_journal_after_a_restart(tmp_path):
    """Phase N7 audit finding (round-8 verification): _closed_pnls starts
    empty on every FundStateActor construction, so a mid-day process
    restart left the daily-loss breaker blind to every trade that closed
    before the restart - real loss from before and after a restart could
    sum to roughly double max_daily_loss before the breaker could ever
    latch. Runs a real backtest (populating the journal with real closed
    trades), then a second, fresh actor instance against the SAME journal
    file (simulating a restart) - its on_start() rehydration must recover
    the first run's trades, not just its own."""
    _fund_state_1, _journal_1, pnls_after_run_1 = _run_multi_trade_backtest(tmp_path)
    assert len(pnls_after_run_1) >= 3, "need real trades from the first run to prove rehydration"

    fund_state_2, _journal_2, pnls_after_both_runs = _run_multi_trade_backtest(tmp_path)

    assert len(pnls_after_both_runs) > len(pnls_after_run_1), (
        "test setup assumption failed: the second run must add its own new closed trades too"
    )
    assert fund_state_2._realized_pnl_since(0) == sum(pnls_after_both_runs), (
        "the second actor's accumulator is missing trades from before its own construction - "
        "on_start()'s journal rehydration did not recover the first run's history"
    )


def test_daily_loss_breaker_latches_after_multiple_closed_trades(tmp_path, monkeypatch):
    """Phase N7 audit finding C2's failure scenario, reproduced directly: a
    cap tight enough that a single ordinary loss breaches it, so the
    breaker either latches on real accumulated loss or it doesn't work at
    all - there is no plausible pass-by-accident here."""
    import wit.nautilus.actor as actor_module
    from wit.config import Config, RiskConfig

    monkeypatch.setattr(actor_module, "CONFIG",
                        Config(risk=RiskConfig(max_daily_loss=0.0001)))  # $5 on $50k equity

    kill_switch = tmp_path / "KILL"
    _fund_state, _journal, journal_pnls = _run_multi_trade_backtest(
        tmp_path, kill_switch_file=kill_switch,
    )

    assert any(pnl < 0 for pnl in journal_pnls), (
        "test setup assumption failed: no losing trade occurred to prove the breaker against"
    )
    assert kill_switch.exists(), (
        "daily-loss breaker never latched despite a real realized loss past the cap"
    )


def test_fractional_kelly_reads_the_real_accumulated_sample(tmp_path, monkeypatch):
    """Phase N7 audit finding H2: the only prior test asserted
    `0.0 < kelly_mult`, which every reachable return value of
    kelly_multiplier() satisfies unconditionally - it could not have caught
    the sample being permanently empty. This pins the actual sample size
    against the journal's independent count, and requires a non-1.0
    multiplier (kelly_min_trades=1 and a real, non-degenerate P&L mix make
    a bare 1.0 no-op implausible unless the sample were empty)."""
    import wit.nautilus.actor as actor_module
    from wit.config import AdaptiveConfig, Config

    monkeypatch.setattr(actor_module, "CONFIG",
                        Config(adaptive=AdaptiveConfig(use_fractional_kelly=True,
                                                        kelly_min_trades=1)))

    fund_state, _journal, journal_pnls = _run_multi_trade_backtest(tmp_path)
    kelly_mult, _drawdown_mult = fund_state.size_multipliers()

    assert len(fund_state._closed_pnls) == len(journal_pnls) > 0, (
        "the actor's own accumulator must match the journal's independent count of closed trades"
    )
    assert kelly_mult != 1.0, (
        "kelly_mult stayed at the disabled/empty-sample default despite a real accumulated sample"
    )
