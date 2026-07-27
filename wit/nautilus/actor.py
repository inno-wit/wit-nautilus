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
daily review 23:55 UTC, weekly dream Sunday 22:30 UTC) are scheduled here but
their bodies are stubs pending Phase N7's `reflection.py`/`dream.py`
orchestration half — `wit/ops/dream.py`'s *state* layer already exists
(Phase N2), so `dream_state` is loaded and exposed now, but nothing yet
*recomputes* it. Timer firing is exercised in tests; the reflection/dream
call it will eventually make is not (there is nothing to call yet).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nautilus_trader.common.actor import Actor, ActorConfig
from nautilus_trader.model.identifiers import Venue

from wit.config import CONFIG
from wit.ops import dream as dream_mod
from wit.risk import adaptive


class FundStateActorConfig(ActorConfig, frozen=True):
    venue: Venue
    account_currency: str = "USD"
    kill_switch_file: str = ""
    dream_state_path: str = ""
    poll_interval_seconds: int = 30


class FundStateActor(Actor):
    def __init__(self, config: FundStateActorConfig) -> None:
        super().__init__(config)
        self._kill_switch_path = Path(config.kill_switch_file or CONFIG.safety.kill_switch_file)
        self._halted = False
        self._halt_reason: str | None = None
        self._kelly_mult = 1.0
        self._drawdown_mult = 1.0
        self._start_of_day_equity: float | None = None
        self._day: object | None = None
        self.dream_state = dream_mod.load(config.dream_state_path or None)

    # -- lifecycle -----------------------------------------------------------
    def on_start(self) -> None:
        self._poll_once()
        self.clock.set_timer(
            name="fund_state_poll",
            interval=timedelta(seconds=self.config.poll_interval_seconds),
            callback=self._on_poll_timer,
        )

    def on_stop(self) -> None:
        self.clock.cancel_timer("fund_state_poll")

    def _on_poll_timer(self, event) -> None:
        self._poll_once()

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
    def _recompute_multipliers(self) -> None:
        acfg = CONFIG.adaptive
        try:
            from nautilus_trader.model.objects import Currency
            currency = Currency.from_str(self.config.account_currency)
            equity_by_ccy = self.portfolio.equity(self.config.venue)
            equity = float(equity_by_ccy[currency]) if equity_by_ccy and currency in equity_by_ccy else None
        except Exception as e:  # noqa: BLE001 - sizing inputs must never crash the actor
            self.log.warning(f"could not read portfolio equity: {type(e).__name__}: {e}")
            equity = None

        now = datetime.now(UTC)
        if self._day != now.date():
            self.reset_daily_state()
            self._day = now.date()
        if self._start_of_day_equity is None:
            self._start_of_day_equity = equity

        if equity is None or self._start_of_day_equity is None:
            self._kelly_mult, self._drawdown_mult = 1.0, 1.0
            return

        realized = equity - self._start_of_day_equity
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

        # Fractional Kelly needs a closed-trade P&L history; that's the
        # journal's job (wit/ops/journal.py), wired once N7's reflection
        # aggregation lands. Off (1.0) until then regardless of the config
        # flag - a silent no-op is safer than a Kelly multiplier computed
        # from no data.
        self._kelly_mult = 1.0

    def size_multipliers(self) -> tuple[float, float]:
        return self._kelly_mult, self._drawdown_mult

    def reset_daily_state(self) -> None:
        """Called at day rollover (00:00 UTC) so the next day's drawdown
        throttle measures against the new day's opening equity, not a stale
        one. A latched daily-loss breach does NOT clear here - matching the
        MT5 build's SafetyMonitor, a breached day must not resume trading
        until a human clears the kill switch."""
        self._start_of_day_equity = None
