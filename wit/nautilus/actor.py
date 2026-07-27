"""``FundStateActor`` — fund-wide state that doesn't belong to any single
instrument's strategy: the kill switch, the daily-loss breaker, adaptive
sizing multipliers, and the dream state (build plan §3 Phase N5).

One instance per `Trader`, shared with every `WitStrategy` by direct Python
reference (see that module's docstring for why config can't carry this).
`WitStrategy._on_decision` calls `is_halted()` before ever building a plan and
`size_multipliers()` when it does — the same fund-wide inputs
`Orchestrator._size_multipliers`/`SafetyMonitor.check` computed once per cycle
in the MT5 build, now computed on a timer instead of once per watchlist loop
(Nautilus has no "cycle"; bars arrive per-instrument, asynchronously).

The three cron-equivalents from the build plan (daily briefing 00:05 UTC,
daily review 23:55 UTC, weekly dream Sunday 22:30 UTC) are scheduled here
(Phase N7), each self-rearming via `Clock.set_timer`'s own `interval`/
`start_time` rather than an external scheduler — Nautilus has no APScheduler
equivalent, and `self.clock` already gives every timer simulated time in a
backtest, real time live, for free. `journal`/`committee`/`alerter` are
plain Python constructor arguments, not `ActorConfig` fields, for the same
reason `WitStrategy` keeps `provider`/`fund_state` out of its config — they
are live objects (an LLM client, a file-backed journal), not the kind of
thing `NautilusConfig` (msgspec) is meant to serialize. `committee` is
optional and duck-typed to `dream.DreamCommittee` (only
`LiveCommitteeProvider` implements `.dream()` today — `StubPolicyProvider`/
`ReplayCommitteeProvider` don't, so backtests/sweeps that construct this
actor without one simply skip the weekly cycle rather than raise).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from nautilus_trader.common.actor import Actor, ActorConfig
from nautilus_trader.model.identifiers import Venue

from wit.config import CONFIG
from wit.ops import dream as dream_mod
from wit.ops.alerts import Alerter
from wit.ops.journal import Journal
from wit.ops.reflection import Reflection
from wit.risk import adaptive

if TYPE_CHECKING:
    from wit.ops.dream import DreamCommittee


def _next_daily(now: datetime, hour: int, minute: int) -> datetime:
    """The next occurrence of ``hour:minute`` UTC at or after ``now`` -
    strictly after if ``now`` already sits exactly on it, so a timer never
    fires twice for the same moment."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _next_weekly(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    """The next occurrence of ``weekday`` (Monday=0 .. Sunday=6) at
    ``hour:minute`` UTC at or after ``now``."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


# How long record_realized_pnl() keeps a closed trade in memory. Generous on
# purpose - closures on an 8-symbol watchlist are infrequent, so the memory
# cost of erring high is negligible, while erring low would silently starve
# the daily-loss breaker or a wide Kelly/dream lookback window (Phase N7
# audit findings C2/H2 - see record_realized_pnl's own docstring for why
# this accumulator exists instead of a Cache query).
_PNL_RETENTION_DAYS = 90


class FundStateActorConfig(ActorConfig, frozen=True):
    venue: Venue
    # kill_switch_file and dream_state_path have NO default (Phase N5 audit
    # finding F4, extended to dream_state_path by the Phase N7 audit's
    # finding M1): a caller that omits either would previously fall back to
    # the real production path (CONFIG.safety.kill_switch_file /
    # CONFIG.dream.state_path). A backtest or parameter sweep that trips the
    # daily-loss breaker (routine, at 0.5%-per-trade risk over any real
    # drawdown) or completes a weekly dream cycle would then write LIVE
    # state - halting the actual fund, or overwriting the real lessons file
    # with a sweep cell's fabricated history. Every caller, including tests,
    # must now decide both paths explicitly.
    kill_switch_file: str
    dream_state_path: str
    account_currency: str = "USD"
    poll_interval_seconds: int = 30


class FundStateActor(Actor):
    def __init__(
        self,
        config: FundStateActorConfig,
        journal: Journal | None = None,
        committee: DreamCommittee | None = None,
        alerter: Alerter | None = None,
    ) -> None:
        super().__init__(config)
        self._kill_switch_path = Path(config.kill_switch_file)
        self._halted = False
        self._halt_reason: str | None = None
        self._kelly_mult = 1.0
        self._drawdown_mult = 1.0
        self._start_of_day_equity: float | None = None
        self._day: object | None = None
        self._closed_pnls: list[tuple[int, float]] = []  # (ts_closed_ns, realized_pnl)
        self.dream_state = dream_mod.load(config.dream_state_path)
        self.journal = journal or Journal(CONFIG.journal_path)
        self.committee = committee
        self.alerter = alerter or Alerter.from_env()

    # -- realized P&L accumulator ---------------------------------------------
    def record_realized_pnl(self, realized_pnl: float, ts_ns: int) -> None:
        """Called directly by each ``WitStrategy.on_position_closed`` -
        ``Cache.positions_closed()`` cannot be used for this (Phase N7 audit
        findings C1/C2). Under ``OmsType.NETTING`` - the only OMS this system
        runs, hard-coded by the IBKR execution client - Nautilus derives
        ``position_id`` as a constant ``f"{instrument_id}-{strategy_id}"``,
        and re-entering a symbol evicts its prior closed position from
        ``Cache``'s closed-position index (confirmed against the installed
        adapter/cache sources and by executing a real multi-trade backtest -
        both the daily-loss breaker and the reflection/dream P&L pipeline
        read 0.0 / nothing after real closed trades). This accumulator is
        fed once per genuine round trip, in ``WitStrategy``'s own real-time
        event, and never gets evicted."""
        self._closed_pnls.append((ts_ns, realized_pnl))
        self._prune_closed_pnls()

    def _prune_closed_pnls(self) -> None:
        cutoff_ns = int(
            (self.clock.utc_now() - timedelta(days=_PNL_RETENTION_DAYS)).timestamp() * 1_000_000_000
        )
        self._closed_pnls = [(ts, pnl) for ts, pnl in self._closed_pnls if ts >= cutoff_ns]

    def _rehydrate_closed_pnls(self) -> None:
        """Recover recent realized P&L from the journal at startup (Phase N7
        audit finding, round 8): ``_closed_pnls`` starts empty on every
        actor construction, so a mid-day process restart left the
        daily-loss breaker blind to every trade that closed before the
        restart - realized loss from before and after a restart could sum
        to roughly double ``max_daily_loss`` before the breaker could ever
        latch. The journal already durably records every closure
        (``WitStrategy.on_position_closed``), so this reads it back rather
        than inventing a second persistence mechanism. Depends on journal
        entries being stamped with the caller's own clock, not wall-clock
        time (see ``Journal.log_event``'s ``ts=`` parameter) - otherwise
        this filter would silently be comparing the wrong time domain in a
        backtest, the same bug class this phase's H1 fix addressed."""
        cutoff = self.clock.utc_now() - timedelta(days=_PNL_RETENTION_DAYS)
        for rec in self.journal.entries_since(cutoff):
            if rec.get("type") != "event" or rec.get("kind") != "position_closed":
                continue
            pnl, ts_str = rec.get("realized_pnl"), rec.get("ts")
            if pnl is None or not ts_str:
                continue
            try:
                ts_ns = int(datetime.fromisoformat(ts_str).timestamp() * 1_000_000_000)
            except ValueError:
                continue
            self._closed_pnls.append((ts_ns, float(pnl)))
        self._prune_closed_pnls()

    # -- lifecycle -----------------------------------------------------------
    def on_start(self) -> None:
        self._rehydrate_closed_pnls()
        self._poll_once()
        self.clock.set_timer(
            name="fund_state_poll",
            interval=timedelta(seconds=self.config.poll_interval_seconds),
            callback=self._on_poll_timer,
        )
        now = self.clock.utc_now()
        # fire_immediately=True on all three: set_timer's default fires the
        # FIRST event at start_time + interval, not at start_time itself
        # (confirmed against the installed nautilus_trader - without this,
        # the computed "next occurrence" is silently skipped once, e.g. a
        # weekly timer due this Sunday would instead first fire next
        # Sunday). With it, start_time is exactly the next real occurrence
        # _next_daily/_next_weekly computed, and every occurrence after
        # that is start_time + N * interval.
        self.clock.set_timer(
            name="daily_briefing",
            interval=timedelta(days=1),
            start_time=_next_daily(now, 0, 5),
            callback=self._on_daily_briefing,
            fire_immediately=True,
        )
        self.clock.set_timer(
            name="daily_review",
            interval=timedelta(days=1),
            start_time=_next_daily(now, 23, 55),
            callback=self._on_daily_review,
            fire_immediately=True,
        )
        self.clock.set_timer(
            name="weekly_dream",
            interval=timedelta(days=7),
            # Sunday = weekday 6 (Monday=0). FX is already closed all day
            # Sunday, so nothing competes with a live trading cycle.
            start_time=_next_weekly(now, 6, 22, 30),
            callback=self._on_weekly_dream,
            fire_immediately=True,
        )

    def on_stop(self) -> None:
        # No manual timer teardown here (Phase N7 audit finding L1):
        # Actor._stop() already calls self._clock.cancel_timers()
        # unconditionally right after on_stop() returns
        # (nautilus_trader/common/actor.pyx, confirmed against the installed
        # source). A by-name loop here adds nothing and adds a failure mode
        # instead - Clock.cancel_timer(name) raises KeyError on an unknown
        # name, so if on_start ever raised partway through registering the
        # four timers, this loop would mask the real startup error with a
        # KeyError during shutdown.
        pass

    def _on_poll_timer(self, event) -> None:
        self._poll_once()

    # -- daily/weekly cron-equivalents ----------------------------------------
    def _on_daily_briefing(self, event) -> None:
        """Morning brief: account state, positions, watchlist, mode. A
        failure here must not take down the trading strategies, so it only
        logs (matching the MT5 build's ``FundScheduler.daily_briefing``)."""
        try:
            equity = self._read_equity()
            equity_line = (f"Equity    {equity:,.2f} {self.config.account_currency}"
                          if equity is not None else "Equity    (unavailable)")
            open_positions = len(self.cache.positions_open())
            lines = [
                f"== Daily briefing · {self.clock.utc_now():%Y-%m-%d} UTC ==",
                f"Account   {self.config.venue} (PAPER)",
                equity_line,
                f"Kill sw   {'ENGAGED' if self._halted else 'clear'}",
                f"Positions {open_positions}/{CONFIG.risk.max_concurrent_positions}",
                f"Watchlist {', '.join(CONFIG.watchlist)}",
            ]
            self.alerter.send("\n".join(lines))
        except Exception as e:  # noqa: BLE001 - a briefing outage must not halt trading
            self.journal.log_event("briefing_error", f"{type(e).__name__}: {e}",
                                   ts=self.clock.utc_now())

    def _on_daily_review(self, event) -> None:
        """End-of-day Reflection summary: realized P&L vs. the thesis that
        opened each closed trade. A failure here must not take down the
        trading strategies, so it only logs."""
        try:
            review = Reflection(self.journal).review(days=1, now=self.clock.utc_now())
            self.alerter.send(Reflection.format(review))
        except Exception as e:  # noqa: BLE001 - a review outage must not halt trading
            self.journal.log_event("review_error", f"{type(e).__name__}: {e}",
                                   ts=self.clock.utc_now())

    def _on_weekly_dream(self, event) -> None:
        """Weekly self-review (build plan Phase N7) — Reflection's aggregate
        stats become a handful of lessons the committee will weigh, never
        obey, on future cycles. Skipped (not an error) when no committee
        capable of ``.dream()`` was wired in - true for every backtest/sweep
        using ``StubPolicyProvider``/``ReplayCommitteeProvider``."""
        if self.committee is None or not hasattr(self.committee, "dream"):
            self.journal.log_event("dream_skipped", "no dream-capable committee configured",
                                   ts=self.clock.utc_now())
            return
        try:
            # self.config.dream_state_path - required, no fallback default
            # (Phase N7 audit finding M1) - so this always writes/reads the
            # path this specific actor was deliberately configured with,
            # never CONFIG.dream.state_path's production default.
            dream_cfg = replace(CONFIG.dream, state_path=self.config.dream_state_path)
            state = dream_mod.run(self.committee, self.journal, dream_cfg,
                                  now=self.clock.utc_now())
            self.dream_state = state
            self.alerter.send(dream_mod.format_digest(state))
        except Exception as e:  # noqa: BLE001 - a dream-cycle outage must not halt trading
            self.journal.log_event("dream_error", f"{type(e).__name__}: {e}",
                                   ts=self.clock.utc_now())

    def _poll_once(self) -> None:
        self._check_kill_switch()
        self._recompute_multipliers()

    # -- kill switch -----------------------------------------------------------
    def _check_kill_switch(self) -> None:
        if self._kill_switch_path.exists():
            if not self._halted:
                self.log.warning(f"Kill switch engaged: {self._kill_switch_path}")
            self._halted = True
            try:
                content = self._kill_switch_path.read_text(encoding="utf-8").strip()
            except OSError:
                content = ""
            self._halt_reason = content or "kill switch engaged"
            return
        # File cleared by an operator (wit halt/resume, N7) -> resume.
        self._halted = False
        self._halt_reason = None

    def engage_kill_switch(self, reason: str = "manual") -> None:
        self._kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
        self._kill_switch_path.write_text(reason, encoding="utf-8")
        self._halted = True
        self._halt_reason = reason

    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    # -- daily-loss breaker + adaptive multipliers ---------------------------
    def _read_equity(self) -> float | None:
        try:
            from nautilus_trader.model.objects import Currency
            currency = Currency.from_str(self.config.account_currency)
            equity_by_ccy = self.portfolio.equity(self.config.venue)
            return float(equity_by_ccy[currency]) if equity_by_ccy and currency in equity_by_ccy else None
        except Exception as e:  # noqa: BLE001 - sizing inputs must never crash the actor
            self.log.warning(f"could not read portfolio equity: {type(e).__name__}: {e}")
            return None

    def _realized_pnl_since(self, since_ns: int) -> float:
        """Sum of realized P&L (incl. commissions) since ``since_ns``, from
        ``self._closed_pnls`` (Phase N7 audit finding C2) - NOT from
        ``self.cache.positions_closed()``, which the Phase N6 audit's finding
        F3 fix (dropping a ``venue=`` filter) made non-empty but did not make
        complete. Under ``OmsType.NETTING``, ``Cache`` can hold at most one
        closed position per instrument - it is overwritten and evicted from
        the closed index the instant that instrument is re-entered
        (confirmed against the installed ``cache.pyx`` and by executing a
        real multi-trade backtest: after fourteen completed round trips on
        one symbol, this query still returned 0.0). ``record_realized_pnl``
        is fed once per genuine closure and never evicted, so the breaker
        can now actually observe a day's real cumulative loss."""
        return sum(pnl for ts, pnl in self._closed_pnls if ts >= since_ns)

    def _recompute_multipliers(self) -> None:
        acfg = CONFIG.adaptive
        # self.clock.utc_now(), not datetime.now(UTC) (Phase N5 audit finding
        # F1/F3): in a backtest this clock is simulated time, potentially
        # months away from wall-clock "now" - using the real system clock here
        # meant _start_of_day_equity never rolled over across a multi-day
        # backtest, silently turning "daily loss" into "cumulative loss since
        # the run started". self.clock is correct in both backtest and live.
        now = self.clock.utc_now()
        if self._day != now.date():
            self.reset_daily_state()
            self._day = now.date()
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        day_start_ns = int(day_start.timestamp() * 1_000_000_000)

        equity = self._read_equity()
        if self._start_of_day_equity is None:
            self._start_of_day_equity = equity

        if equity is None or self._start_of_day_equity is None:
            self._kelly_mult, self._drawdown_mult = 1.0, 1.0
            return

        realized = self._realized_pnl_since(day_start_ns)
        if acfg.drawdown_throttle:
            self._drawdown_mult = adaptive.drawdown_multiplier(
                realized, self._start_of_day_equity, CONFIG.risk.max_daily_loss,
                acfg.drawdown_mult_floor,
            )
        else:
            self._drawdown_mult = 1.0

        breached = (realized < 0
                   and abs(realized) >= CONFIG.risk.max_daily_loss * self._start_of_day_equity)
        # Latches via the same kill-switch file the manual/poll path uses,
        # matching the MT5 build's SafetyMonitor: a breached day must not
        # silently resume trading, including across a process restart.
        if breached and not self._halted:
            self.engage_kill_switch("daily loss breaker")
            self.log.warning(
                f"daily loss breaker: realized {realized:.2f} vs start-of-day equity "
                f"{self._start_of_day_equity:.2f} (cap {CONFIG.risk.max_daily_loss:.1%})"
            )

        # Fractional Kelly needs a closed-trade P&L history - wired here now
        # that N7's reflection aggregation exists. kelly_multiplier() itself
        # returns 1.0 (no effect) whenever use_fractional_kelly is off or the
        # sample is under kelly_min_trades, so this is a strict superset of
        # the prior hard-coded 1.0 when the feature flag stays at its
        # default. Reads self._closed_pnls, not cache.positions_closed() -
        # same reasoning as _realized_pnl_since above (Phase N7 audit finding
        # H2: under NETTING the cache can hold at most one closed position
        # per instrument, so an 8-symbol watchlist could never reach
        # kelly_min_trades=30 regardless of how much real history existed).
        lookback_ns = int(
            (now - timedelta(days=acfg.kelly_lookback_days)).timestamp() * 1_000_000_000
        )
        pnls = [pnl for ts, pnl in self._closed_pnls if ts >= lookback_ns]
        self._kelly_mult = adaptive.kelly_multiplier(
            adaptive.kelly_stats(pnls), CONFIG.risk.risk_per_trade, acfg,
        )

    def size_multipliers(self) -> tuple[float, float]:
        return self._kelly_mult, self._drawdown_mult

    def reset_daily_state(self) -> None:
        """Called at day rollover (00:00 UTC) so the next day's drawdown
        throttle measures against the new day's opening equity, not a stale
        one. A latched daily-loss breach does NOT clear here - matching the
        MT5 build's SafetyMonitor, a breached day must not resume trading
        until a human clears the kill switch."""
        self._start_of_day_equity = None
