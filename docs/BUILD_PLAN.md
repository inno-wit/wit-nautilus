# Wit Hedge Fund — NautilusTrader build plan (second, Linux-native build)

## Context

This started as "set up Wit Hedge Fund for testing on a Linux VPS." The existing build is
MT5-only, and MT5's Python package is Windows-only — a first plan (`docs/LINUX_DEPLOYMENT_PLAN.md`
in the MT5 repo) worked around that with Wine + a bridge. Exploring "what if we used Alpaca
instead" surfaced that Alpaca has no forex/metals/index coverage, and exploring "what about
NautilusTrader" surfaced that NautilusTrader has *no* MT5 adapter and only an unbuilt RFC for
Alpaca — so neither is a drop-in swap.

The user's actual intent, confirmed across this conversation: run **two independent builds**.
The original MT5 build keeps running on a **Windows VPS** (purchased separately, out of scope
here) exactly as it does today — no changes. This plan covers a **second, brand-new, separate
repository** that ports the same decision-making pipeline (the LLM bull/bear/PM committee, the
deterministic quant desks, the risk/consensus gate) onto **NautilusTrader**, executing through
**Interactive Brokers** first (a real, stable NautilusTrader adapter), with **vectorbt** as an
offline research/parameter-sweep layer, deployed via **Docker Compose** on the existing Linux
VPS (`169.58.78.135`, Ubuntu 24.04.4, 4 vCPU / 7.8 GiB / 96 GB, key-based SSH already working).
Building a NautilusTrader adapter for Alpaca (none exists upstream — open, unassigned RFC
#3374) is an explicit, deferred later phase, after the IBKR build is proven on paper.

Confirmed by the user, not to be re-litigated:
- New, separate git repo (not inside `Wit-Hedge-fund`).
- Reuse the LLM committee pattern (bull/bear researchers → Portfolio Manager), not a
  from-scratch quant-only strategy.
- Phase 1 broker: Interactive Brokers only. Alpaca adapter-building is a later, separate phase.
- Deployment: Docker / Docker Compose on the Linux VPS.

Everything below that touches NautilusTrader or the IB adapter was checked against
`nautilustrader.io/docs/latest` and the adapter's GitHub docs before being included — anything
that couldn't be confirmed is called out explicitly in §4 rather than presented as fact.

---

## 1. Architecture mapping (old MT5 build → new NautilusTrader build)

| Old (MT5 build) | New (NautilusTrader + IBKR) | Notes |
|---|---|---|
| `engine/broker/base.py::BrokerAdapter` (8-method ABC) | **Deleted.** Nautilus's `DataClient`/`ExecutionClient` are the abstraction | The ABC existed to make MT5 swappable; Nautilus already is the swap layer |
| `MT5Adapter.candles(symbol, tf, count)` | `self.request_bars(bar_type, start=…)` at `on_start` → `on_historical_data`; live via `self.subscribe_bars(bar_type)` → `on_bar`; window read from `self.cache.bars(bar_type)` | Push, not pull. `CacheConfig.bar_capacity` defaults to 10,000 bars/type — well above the 750-bar warmup |
| `SymbolSpec` (digits, point, tick_size, tick_value, volume_min/max/step, stops_level) | `Instrument` from `self.cache.instrument(instrument_id)`: `price_precision`, `price_increment`, `size_precision`, `size_increment`, `min_quantity`, `max_quantity`, `lot_size`, `multiplier`, `margin_init`, `margin_maint` | No `tick_value` equivalent — see §1.3 |
| `AccountInfo` (balance/equity/margin_free/leverage) | `self.portfolio.account(venue)` + `self.portfolio.equity(venue)` | Exact balance/free-margin accessor names unconfirmed — §4.4 |
| `AccountInfo.is_demo` | No framework equivalent — assert on IB account id prefix (`DU…`=paper, `U…`=live) + gateway `trading_mode` + port | This is the new home of the `paper_only` lock — see §1.4 |
| `symbol_price() -> (bid, ask)` | `self.cache.quote_tick(instrument_id)` → `.bid_price`/`.ask_price` | Feeds `revalidate_plan` unchanged |
| `place_market_order(sym, side, lots, sl, tp)` | `self.order_factory.bracket(...)` → `self.submit_order_list(orders)` | Entry+SL+TP as one order list |
| `positions()` / `close_position()` | `self.cache.positions_open()` / `self.close_position(position)` | |
| `Orchestrator.run_cycle` (loop over watchlist) | **Gone.** Bars arrive per-instrument; fund-wide state (kelly/drawdown mults, dream state) moves to a `FundStateActor` on a timer | |
| `Orchestrator.process_symbol` | `WitStrategy.on_bar(bar)` body | One strategy instance per instrument |
| `SafetyMonitor.check` | Split three ways: our own pre-submit gate, Nautilus `RiskEngine` `TradingState.HALTED`, `LiveRiskEngineConfig` caps | See §1.4 |
| kill-switch file | File poll on `clock.set_timer` inside `FundStateActor` | Same file-on-disk UX, plus `set_trading_state(HALTED)` as backstop |
| daily-loss breaker (3%, latching) | `FundStateActor` timer: `portfolio.realized_pnl(venue)` vs start-of-day equity → writes kill-switch file, latches | Behaviour preserved exactly |
| `engine/reconcile.py` | Native `LiveExecEngineConfig` reconciliation (`reconciliation_lookback_mins`) | Keep a thin journal-side reconcile for the journal's own view |
| `scheduler.py` bar-close cron | **Deleted.** Bar events are the cadence | The `+20s` offset hack disappears |
| `scheduler.py` daily briefing/review/dream cron | `self.clock.set_timer(...)` / `set_time_alert(...)` in `FundStateActor` | **APScheduler dropped entirely** — Nautilus's clock also works in backtest, so the dream cycle is testable offline |
| `engine/market_hours.py` | Ports as-is (pure `zoneinfo`) | Drop its `CommitteeDecision` import so it stays contract-only |
| `engine/journal.py` (JSONL) | Ports as-is | Nautilus's own cache/msgbus persistence is separate; the journal stays the audit trail |

### 1.2 The event-loop constraint (the thing that shapes everything else)

The committee is 3 rate-limited LLM calls/symbol (~3 min/bar-close across 10 symbols at 10
RPM). `Strategy.on_bar` **must return in microseconds** — Nautilus's docs are explicit that
blocking the event loop causes missed fills and stale data. So:

```
on_bar(bar):
  synchronous, fast: window from cache → technicals/markov/garch.compute → cheap gates
                      (market_hours, prefilter, capacity) → quant_analyst.compute
                      → hand report to a DecisionProvider, return immediately (no await)

on the deliberation callback (off-loop):
  sizing.build_plan(...) → safety re-check → revalidate_plan against current quote
  → order_factory.bracket(...) → submit_order_list(...) → journal.log_decision(...)
```

**`DecisionProvider` is the key new abstraction** — same strategy code runs in backtest, paper,
and live:

| Implementation | Used in | Behaviour |
|---|---|---|
| `LiveCommitteeProvider` | paper/live | `anthropic.AsyncAnthropic`, async rate limiter, off-loop task |
| `ReplayCommitteeProvider` | backtest | Sync lookup in a decision cache keyed by `(instrument_id, bar_ts, report_hash)`; `record` mode calls the API once and writes through |
| `StubPolicyProvider` | CI / sweeps | Deterministic rule, no network, no cost |

The decision cache isn't an optimization — it's the only way a full-fidelity backtest of an
LLM-mediated strategy is affordable to run more than once. Pay the LLM bill once per
`(symbol, bar)`, then sweep *risk* parameters against the cached decisions freely.

One `WitStrategy` instance per instrument, all under one `Trader`. Fund-wide state
(`open_positions_total`, kelly/drawdown mults, dream state) is read from `Cache`/`Portfolio`
and `FundStateActor`, not a per-strategy loop variable.

### 1.3 `tick_value` has no direct Nautilus equivalent

`sizing.py`'s `_lots_for_risk` converts risk-in-currency to lots via `ticks × tick_value`.
Nautilus's `Instrument` gives `price_increment`/`size_increment`/`multiplier`, not MT5's
"account-currency P&L per tick per lot" field directly — but it's computable per instrument
class (US equities: 1 share = 1 unit, trivial; FX spot: quantity in base-currency units, P&L
needs quote→account currency conversion; futures: `multiplier` × price move).

**Decision: port `sizing.py`'s *gates* unchanged**, add a new `instrument_spec.py` shim that
turns a Nautilus `Instrument` into the same `(loss_per_unit, min_qty, qty_step)` shape
`build_plan` already consumes. **Update (Phase N4, as shipped):** `build_plan`'s signature and
one gate's *kind* did change — see the Phase N4 section below for exactly what and why; "byte-
identical" turned out to describe the gate ordering/thresholds/reason-strings, not the function
body verbatim. That's the risk guarantee that actually matters and it did hold; this note exists
so this section stops contradicting the code. `stops_level_points` (MT5's broker-minimum-stop)
has no IB equivalent; replaced with a configured floor (`InstrumentSpec.min_stop_distance`,
default `0.0` = no floor), same "widen the stop, don't reject" behavior. That default needs a
per-instrument value set before Phase N6 goes live — see the Risks table.

### 1.4 Where each safety guarantee lives now

Nautilus's `RiskEngine` does real pre-trade checks (price/quantity precision, notional caps,
order-rate limits) and denies bad orders before they reach the venue — genuinely useful, but
**defence in depth, not a replacement**. It knows nothing about conviction floors, Markov
vetoes, correlation groups, cooldowns, or a daily-loss breaker.

| Guarantee | Primary home | Backstop |
|---|---|---|
| Kill switch | `FundStateActor` timer polls the file → shared flag checked before every submit | `risk_engine.set_trading_state(HALTED)` |
| `paper_only` | Boot-time assertion: refuse to start unless account id starts with `DU` / gateway `trading_mode=="paper"` / paper port | Fail at boot, never at order time |
| Daily-loss breaker (3%, latching) | `FundStateActor` timer vs start-of-day equity → engages kill switch + `HALTED` | `LiveRiskEngineConfig` caps limit blast radius meanwhile |
| Conviction floor / Markov veto / correlation cap / cooldown / spread cap / position caps / margin | `sizing.build_plan`, unchanged | — |
| Entry-drift / SL-TP sanity at send | `sizing.revalidate_plan`, unchanged | `RiskEngine` precision/notional checks |

**Never set `RiskEngineConfig(bypass=True)`** — not in backtest, not in tests.

---

## 2. vectorbt vs. the Nautilus backtester — division of labor

**Nautilus's own backtester is the only backtest of record. vectorbt is an offline parameter
screener for the deterministic desk layer and never produces a headline P&L number.**

Why: this system's P&L is path-dependent in ways a vectorized engine can't express — the
decision is an LLM verdict conditioned on text, not a formula over an array; `build_plan`'s
gates read sequential state (open positions right now, correlation group, cooldown, today's
realized loss); sizing rounds to a venue quantity step, which is exactly where small accounts
lose trades entirely — an effect vectorbt's continuous sizing hides. Running both as P&L
authorities would just produce two numbers that disagree.

**What vectorbt is for:** the desks (`markov`, `garch`, `technicals`) are stateless array math
over OHLCV — vectorbt's exact strength. Sweep `window`/`threshold_k`/`realized_window`/EMA-RSI-ATR
periods and prefilter thresholds offline, across thousands of combinations, with walk-forward
stability checks. Then take the **top 3–5 candidates only** into `BacktestNode` with the real
committee (replayed from the decision cache) and the real risk engine — that's where the number
that matters comes from.

**Operational separation:** vectorbt is a research-image-only dependency (separate `pyproject`
extra, separate Dockerfile target, separate Compose profile never included in default `up`) —
it pulls numba, which pins numpy and shouldn't be able to constrain the process placing orders.

---

## 3. Phases

### Phase N0 — Confirm IBKR + framework facts before writing code

No repo code. Throwaway scripts against IB Gateway (paper account) + reading the pinned
Nautilus version's actual API. Answers needed, in priority order:

1. **Does the paper gateway login require 2FA?** (Live IBKR does.) Determines whether
   unattended restart is even possible — highest-priority item.
2. **Async work from a `Strategy`.** Docs say user code must return quickly but don't document
   the sanctioned way to launch/await background work. **This is the single most important
   unknown in the whole plan** — the off-loop committee design (§1.2) depends on the answer.
3. **DONE (live probe, `reqContractDetails`).** All 7 equities + EURUSD resolve cleanly;
   equities are trivially `minTick=0.01`/`priceMagnifier=1`. See §4 item 10 for the full table.
4. **Partially done.** A single-symbol 260-bar/15m request completed in 1.3s with no pacing
   issue — but this doesn't clear the *concurrent* 10-instrument case (issue #3718's actual
   failure mode). Retest once N5's strategy code exists to drive 10 real concurrent requests.
5. **Not yet tested** — needs a live bar subscription running for at least one bar interval,
   which is more naturally done alongside N5's strategy code than as an isolated probe.
6. **DONE (live probe, raw `placeOrder`).** Bracket order (parent LMT + child LMT-TP + child
   STP-SL) placed on `EURUSD` paper, away from market so it rested rather than filled. IB
   correctly linked both children to the parent via `parentId`, accepted all three, and
   cancelled cleanly. Confirms the parent/child linkage `OrderFactory.bracket()` relies on
   works as expected. Fill behavior itself (not just acceptance) gets its real test in N9 step 6
   ("first paper order, watched") — deliberately not forced here.
7. **Not yet tested** — needs to be observed across an actual ~21:00–21:15 UTC window with a
   live subscription running, so this is an N8/N9 soak finding, not a five-minute probe.
8. **Not yet tested** — same reasoning as 7, needs N5's strategy code to drive both a live and
   backtest timer through the same callback.
9. **DONE, and it's an action item for the account, not code.** Both `LIVE` and `DELAYED`
   market data types failed with error 10089 for equities (`NVDA`/`SMART`/`NASDAQ`) — this
   paper account has **no US equities market data entitlement at all** yet, not even the free
   delayed tier. **FX is unaffected** — `EURUSD`/`IDEALPRO` returned live ticks immediately,
   zero subscription needed. Action before N6: enable equities market data in IBKR Account
   Management (delayed is free and enough for N9's early gates). See §4 item 9 and the Risks
   table for the full writeup.

### Phase N1 — Repo scaffold

New repo, Python 3.12 (Nautilus supports 3.12–3.14; 3.12 is the safe intersection with `arch`
and numba). Structure:

```
wit-nautilus/
├── pyproject.toml            # extras: [ib] [research] [dev]
├── docker/{Dockerfile, compose.yml, compose.research.yml}
├── wit/
│   ├── config.py
│   ├── desks/                 # ported from engine/signals/
│   ├── committee/              # contract.py, provider.py, live.py, replay.py, stub.py, prompts.py
│   ├── risk/                   # sizing.py, adaptive.py (ported), instrument_spec.py (new)
│   ├── ops/                    # journal.py, reflection.py, dream.py, alerts.py, market_hours.py, prefilter.py, safety.py
│   ├── nautilus/                # strategy.py, actor.py, node_live.py, node_backtest.py
│   ├── research/                # vectorbt sweeps, [research] extra only
│   └── cli.py                   # doctor|backtest|sweep|paper|live|halt|resume|status
├── data/                        # journal.jsonl, dream_state.json, KILL_SWITCH, decisions.db
└── tests/
```

CI on push (GitHub Actions, Ubuntu, 3.12). `.env.example` documenting every key.

### Phase N2 — Desks port

Copy `technicals.py`, `markov.py`, `garch.py`, `market_intel.py`, `quant_analyst.py`,
`contract.py`, `prefilter.py`, `market_hours.py` — import paths only change, plus decoupling
`market_hours.py` from its `CommitteeDecision` import (return a reason string instead).

**Gate: byte-identical `Technicals`/`MarkovSignal`/`GarchSignal`/`as_prompt_block()` output**
for the same input DataFrame, verified against fixtures captured from the MT5 repo. If the
prompt block differs, the committee is being asked a different question and every downstream
comparison is invalid.

### Phase N3 — Committee port + `DecisionProvider`

**Already landed in N2** (`wit/committee/contract.py`): the `CommitteeDecision` dataclass,
`.abstain()`, `distinctiveness()`, `model`/`served_model` fields — pulled forward because
`wit/ops/prefilter.py` needed it to construct synthetic HOLDs, and confirmed by the Phase N2
audit to have zero LLM/network dependencies. Directly tested in
`tests/test_committee_contract.py`. What's left here is everything that actually talks to a
model: the bull/bear/PM prompts verbatim, forced-tool-use schema unchanged. Split the
client into `provider.py` (protocol), `live.py` (async Anthropic + async `_RateLimiter` —
**never a thread-blocking `time.sleep` on the event loop**), `replay.py` (SQLite decision cache
keyed by `(instrument_id, bar_ts_ns, sha256(prompt_block))`, `strict`/`record` modes),
`stub.py` (deterministic, no network).

**Provider decision: default to direct Anthropic, keep `base_url` configurable.** The MT5
build's own notes record its free-tier gateway (NaraRouter) silently serving a different model
than requested — real money reasons to not default a live-order-placing system to a free,
substitution-prone gateway. `served_model` stays logged either way, since it's the only way to
detect substitution if a gateway is used later.

**Gate:** confirm every failure mode (timeout, malformed tool call, missing required field,
non-numeric field, 429, no key) returns `abstain` and never raises. **As actually run** (Phase
N3 audit finding F9): via a stubbed Anthropic client over synthetic `make_bars`-derived
reports (`tests/test_committee_live.py`), not literal MT5-journal fixtures replayed through a
live client — the MT5 build's journal doesn't carry raw Anthropic response shapes to replay,
only the already-parsed `CommitteeDecision`. The failure-mode coverage itself is not weaker for
it (same sentinels the MT5 suite used: empty content, no tool call, missing key, non-numeric
field, raised exception), but if a literal journal-replay gate is wanted later, it needs new
tooling to capture raw API responses, not just decisions.

**Landed via the Phase N3 audit, not in the original design (findings F1-F3, blocking):**
`LLMConfig.rpm_limit` was missing (the N2 config port dropped it, N3's `live.py` referenced it
anyway — `LiveCommitteeProvider` could not be constructed until this was fixed); `_RateLimiter`
and `ReplayCommitteeProvider`'s SQLite connection were not thread-safe, which mattered
immediately because `run_in_executor` dispatches to a *shared, multi-worker* thread pool (see
`provider.py`'s docstring) — several symbols' committee calls can run concurrently, not one at
a time. Both are fixed; **Phase N5 must not reintroduce single-threaded assumptions** when
wiring `WitStrategy` to a `DecisionProvider`.

**Owed to N5 design (finding F11, non-blocking):** NautilusTrader's kernel sets the committee's
thread pool as the event loop's *default* executor — every `run_in_executor(None, ...)`
anywhere in nautilus_trader and the IB adapter draws from the same pool the committee occupies.
Three sequential Anthropic calls (up to 90s each) per symbol per bar is a long hold on a shared
resource. Consider registering a dedicated executor for committee work in `WitStrategy`/
`FundStateActor` rather than relying on the shared default.

### Phase N4 — Risk/sizing port

1. `adaptive.py` — verbatim (pure math).
2. `instrument_spec.py` (new) — `spec_for(instrument, ...) -> InstrumentSpec` using the Phase
   N0 instrument table.
3. `sizing.py` — port. **As actually shipped (Phase N4 audit, superseding this section's
   original text below):** gate ordering, `MARKOV_VETO_THRESHOLD`, and every blocked-reason
   string are unchanged, but `SymbolSpec` → `InstrumentSpec` was not the only substantive
   change — `RiskConfig.max_spread_points` (an MT5 "points" concept with no IBKR equivalent)
   was dropped entirely, `spread_points: int` became `spread: float` in native price units
   throughout `build_plan`/`revalidate_plan`, and `revalidate_plan`'s live-spread check changed
   from a points-only test to a pct-only test (a substitution, not a translation — the original
   plan text below undersold this). A malformed-quote guard (`last_close <= 0` / `spread < 0`)
   was added after the audit found the pct-only gate fails open on a broken quote, which the
   points cap had been incidentally protecting against. See `wit/risk/sizing.py`'s and
   `wit/risk/instrument_spec.py`'s module docstrings for the full reasoning.

~~**Gate:** the MT5 repo's `sizing` test suite passes against the new spec type with only
fixture construction changed. Any test that needs rewriting (not just re-fixturing) means a
risk guarantee moved — stop and explain before proceeding.~~ **As actually applied:** this
stop condition was honored in substance, not literally — the unit-system changes above forced
real (not fixture-only) changes in `tests/test_sizing.py`, and each one is explained in that
file's own docstring and in the N4 commit message rather than silently absorbed. The plan text
just wasn't updated to say so until this audit-fix pass; treat the paragraph above as the
current source of truth for what N4 actually did.

### Phase N5 — Strategy + Actor, backtest mode first

Backtest before live: deterministic, no gateway, no money.

**`WitStrategy(Strategy)`**: `on_start` resolves the instrument, requests warmup bars, then
subscribes to live bars + quote ticks (in that order, so live starts behind a warm cache).
`on_bar` is the fast synchronous path from §1.2. `_on_decision(...)` (the deliberation
callback) does `build_plan` → safety re-check → `revalidate_plan` → bracket order → journal.
`on_order_filled`/`on_position_closed` journal fills and exit timestamps — **this makes the
cooldown genuinely exit-aware for free** (the MT5 build's cooldown is entry-based because MT5
exits are broker-side and invisible to its journal). `on_stop` cancels working orders but does
not close positions (same posture as the MT5 build).

**`FundStateActor(Actor)`**: kill-switch poll timer, daily-loss-breaker timer, adaptive-mult
recompute timer, and the three cron-equivalents (`clock.set_time_alert`) — daily briefing
00:05 UTC, daily review 23:55 UTC, weekly dream **Sunday 22:30 UTC** (moved from the MT5
build's 21:00 to avoid IB's ~21:00–21:15 UTC daily gateway restart window).

**Owed from N2 (audit finding F8): DONE.** `WitStrategy._on_bar_work` reconstructs
`market_closed_hold`'s two marker fields (`model="market_hours"`,
`detail={"market_closed": True, "reason": closed_reason}`) at the call site, as planned.

**Gate: DONE, but not as originally scoped.** A ≥3-month backtest was deferred — 150 bars was
enough to exercise the full `on_bar`/`_on_bar_work`/`_on_decision`/bracket-order/journal path
against a real `BacktestEngine` (`tests/test_strategy_backtest.py`), which is what this gate
actually needed to prove for N5. A multi-month run with realistic P&L is N9's job, not N5's — this
repo doesn't have historical bar data wired up yet (that's part of N6/N9). `ReplayCommitteeProvider`
in `record` mode against a real committee is also deferred to N9 for the same reason (needs a real
`ANTHROPIC_API_KEY` and a longer run to be worth the cost).

**Phase N5 audit findings (all fixed in this commit unless noted):**
- **F1 (critical):** the daily-loss breaker read `datetime.now(UTC)` — wall-clock time — while
  everything it measures (bars, position closes) is `self.clock.utc_now()` (simulated time in a
  backtest, potentially months adrift from wall-clock). It also differenced `Portfolio.equity`
  (includes *unrealized* P&L for a margin account) where MT5's `SafetyMonitor` uses strictly
  *realized* closed P&L. Fixed: `FundStateActor` now uses `self.clock.utc_now()` for day
  rollover and sums `cache.positions_closed()`'s `realized_pnl` since simulated midnight.
- **F2 (high):** `_on_decision` could submit an order after `on_stop` had already run (the
  committee call budgets up to ~90s, off-loop, with no re-check before submit). Fixed: re-checks
  `self.is_running` and `fund_state.is_halted()` immediately before `_submit`.
- **F3 (high):** the post-exit cooldown used the same wall-clock/simulated-clock mismatch as F1,
  making it inert (0/50 in the audit's measurement) in every backtest. Fixed alongside F1.
- **F4 (high):** `FundStateActorConfig.kill_switch_file` defaulted to the real production path
  (`CONFIG.safety.kill_switch_file`) — a backtest or sweep that trips the (previously-buggy) daily
  loss breaker would write the live kill switch. Fixed: the field is now required, no default.
- **F5 (medium, deferred to N6):** `_submit`'s bracket passes `entry_price` but never sets
  `entry_order_type`, which defaults to `OrderType.MARKET` — a MARKET order never reads
  `entry_price` (confirmed against `OrderFactory`'s source), so the argument is dead and the fill
  can land up to `max_entry_slippage_pct` away from the price the stop/TP distance was sized
  against. Decide LIMIT+GTD vs. MARKET-with-fill-anchored-stops once N6 makes real IB fill
  behavior observable; also confirm `tp_post_only=True` (a factory default) against IB.
- **F6 (medium): DONE.** The backtest test suite's 3-tests-together crash was misdiagnosed as
  host memory pressure; the real cause (confirmed by reproducing with zero project code) is
  nautilus_trader's Rust logger being a process-global singleton that panics on a second `init`.
  Fixed with `BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True))` in the test fixture.
- **F7 (medium): DONE.** `on_bar`'s "must return in microseconds" claim was false — `garch.compute`
  alone measured ~1s cold directly on the event loop. Fixed: `on_bar` now does nothing but
  `run_in_executor(self._on_bar_work, ...)`; the desk computation moved into `_on_bar_work`,
  which runs off-loop in live and inline (same as before) in backtest.
- **F8 (medium): DONE.** `client_order_id` was computed in `_submit`'s return value but never
  threaded into `log_decision`'s own field (only into the nested `order` dict) — fixed. `_default`
  in `wit/ops/journal.py` also now handles multi-element numpy arrays (F12) before falling back to
  `.item()`, which raises on anything but a size-1 array.
- **F9 (low, accepted as a design constraint, not fixed):** `WitStrategy.__init__` takes
  `provider`/`fund_state` as extra constructor args beyond Nautilus's own `config`-only
  convention. `BacktestEngine.reset()` retains instances (safe), but
  `StrategyFactory.create`/`ImportableStrategyConfig` (the config-driven path
  `TradingNodeConfig(strategies=[...])` normally uses) calls `strategy_cls(config=config)` and
  cannot construct this class. **Phase N6 must assemble the `Trader` manually**
  (`trader.add_strategy(WitStrategy(config, provider, fund_state))`), not via the config-driven
  factory path — write this down in N6's own section once that code exists.
- **F10/F11 (low, not reachable on the current watchlist, not fixed):** `open_positions_symbol`
  filters by venue-qualified `instrument_id` while the correlation-group check keys on bare
  `.symbol.value` — two venues carrying one ticker would bypass the per-symbol cap. Separately,
  `open_symbols` collects Nautilus's FX form (`"EUR/USD"`) which never equals
  `config.symbol`'s MT5 form (`"EURUSD"`), so a self-exclusion check in a future FX correlation
  group would silently miscount. Neither is reachable today (single-venue watchlist, no FX
  correlation group configured) — revisit if either changes.

### Phase N6 — IBKR wiring: paper first

`TradingNode` with `InteractiveBrokersDataClientConfig`/`InteractiveBrokersExecClientConfig`
(`ibg_host`, `ibg_port` — **4002 paper / 4001 live**, `ibg_client_id`, `account_id`,
`use_regular_trading_hours`, `market_data_type`). The `paper_only` boot assertion (§1.4) lives
here, before `node.build()`. **Don't use `DockerizedIBGatewayConfig`** on the VPS — it needs the
Docker socket mounted into the trading container for no benefit when Compose can own the
gateway's lifecycle directly; use explicit host/port against a sibling `ib-gateway` service.

**As shipped (`wit/nautilus/node_live.py`):** `assert_paper_only()` checks `WIT_PAPER_ONLY`,
`IBG_PORT` (must be 7497 TWS-paper or 4002 Gateway-paper — 7496/4001 are refused unconditionally),
and `TWS_ACCOUNT`'s `DU` prefix, all before any socket opens. `build_config()` registers one IB
data client + one IB exec client, with an empty `strategies=[]`/`actors=[]` — per N5 audit
finding F9, `WitStrategy`/`FundStateActor` are added manually via
`node.trader.add_actor()`/`add_strategy()` after `node.build()`, since Nautilus's config-driven
`StrategyFactory` can't construct a class that needs a live `DecisionProvider`/`FundStateActor`
reference. The account is registered under IB's own fixed pseudo-venue (`IB_VENUE` =
`"INTERACTIVE_BROKERS"`, confirmed against the installed adapter) — separate from the
per-instrument routing venues (`SMART` for equities, `IDEALPRO` for FX) — so `FundStateActor`
sees one fund-wide equity figure across both asset classes, not a partial per-exchange view.
`INSTRUMENT_IDS` maps the N0-confirmed 8-symbol watchlist to Nautilus/IB instrument-id strings
directly (no dynamic resolution yet — matches the plan's "no options, futures, or crypto" scope).

**Gate: NOT YET RUN** — a live, watched connection to TWS/Gateway paper is still Phase N7+ work,
deliberately not attempted unattended in N6 (see below). It's the first point in the whole build
where code would actually talk to a live brokerage connection and could place a real order, which
crosses from "write and test code" into "operate a connected trading system."

**Phase N6 audit — the safety lock holds, the trading path underneath it didn't (all fixed in a
follow-up commit).** `assert_paper_only()` itself passed adversarial review with no bypass found.
But the first read-only, order-free verification against the live TWS paper session (contract
details only — the exact kind of check the "don't operate the system" caution should *not* have
covered, and the audit called this out directly) found the hard-coded instrument mapping was
wrong for 7 of 8 symbols, and three more defects meant no order could ever have reached IB even
with correct instruments:
- **NASDAQ, not SMART, is the primary-exchange venue IB needs.** The venue component of an IB
  equity `InstrumentId` is that instrument's *primary exchange*; order routing to IB is always
  SMART regardless and is not something `InstrumentId` encodes. `"NVDA.SMART"` asked IB for a
  contract whose *primary exchange* is `"SMART"`, which doesn't exist — confirmed live (error 200
  on all seven equities); re-probed as `.NASDAQ`, all seven resolved to exactly one contract each.
- **The bar-type string duplicated its step token** (`f"{ib_id}-1-{_bar_step(...)}-..."` where
  `_bar_step` already returns `"1-HOUR"`), producing a bar type whose instrument id parsed as
  `"NVDA.NASDAQ-1"` — a phantom instrument `request_bars` silently can't find, so the warmup
  callback never fires and the strategy never subscribes to anything. `build_strategies` now
  asserts `bar_type.instrument_id == instrument_id` to make this class of bug loud again.
- **The account lookup used the wrong venue.** `WitStrategy._account_snapshot()` looked the
  account up under `instrument_id.venue` (`NASDAQ`/`IDEALPRO`) — for a single-venue backtest that
  happens to work, but IB registers the account under its own fixed pseudo-venue (`IB_VENUE`),
  never under an instrument's exchange-routing venue. Every decision died at
  `"no_account_snapshot"` before `build_plan` was ever reached. Fixed by adding
  `WitStrategyConfig.account_venue` (defaults to the instrument venue, so N5's backtest is
  unaffected; IB wiring passes `IB_VENUE` explicitly).
- **The daily-loss breaker's realized-P&L query was structurally guaranteed to return empty** —
  `FundStateActor` filtered `cache.positions_closed(venue=self.config.venue)` by the *account*
  venue, but `Cache` indexes positions by *instrument* venue. The named §1.4 guarantee ("behaviour
  preserved exactly") was silently disabled, not preserved. Fixed by dropping the venue filter
  (safe under this system's single-executor-per-account rule).
- **EURUSD requested `TRADES` bars** (the adapter maps `PriceType.LAST → "TRADES"`), which IB
  doesn't provide for CASH/FX contracts — needs `MID`. `INSTRUMENT_IDS` now carries
  `(instrument_id, price_type)` per symbol so adding a new symbol forces the asset-class decision.

Also fixed: `market_data_type` is now set explicitly (was left at the adapter default, silently
ignoring N0's confirmed equity-entitlement gap); `build_config()` asserts `paper_only` itself
rather than trusting only its caller; `build_node()` constructs every fallible non-IB object
(the committee provider, which fails loudly on missing LLM config) *before* `node.build()` and
disposes the node on any failure, rather than leaking an undisposed kernel if construction failed
partway through after IB clients were already built.

**Before anyone calls `node.run()` against live TWS** (a checklist, separate from and additional
to the fixes above): (1) enable US equity market data on the paper account in IBKR Account
Management — N0's confirmed error-10089 gap, nothing downstream works without it; (2) after the
exec client connects, add a post-connect re-assertion that every account IB's `managedAccounts`
actually reports starts with `DU` — this converts the paper guarantee from "the operator
configured it correctly" to "the broker confirmed it," closing the one residual gap the audit
found (a live TWS session could technically be configured to listen on a paper port); (3)
populate `.env` deliberately (`IBG_PORT=7497`, `TWS_ACCOUNT=<the real DU-prefixed id>`); (4) dry
run `build_node()` alone first (opens no socket — `TradingNode.build()`'s body is just client
construction, the actual connection happens in `run()`) and confirm it returns without raising;
(5) then run attended, one symbol first, with the kill switch pre-armed, confirming in order:
client connects → instruments resolve → warmup bars land in cache → live bars reach `on_bar` →
`_account_snapshot()` returns a real equity figure → only then let a decision reach `_submit`;
(6) verify the (now-fixed) daily-loss breaker for real by closing a losing paper position and
confirming the kill-switch file is written and latches across a restart.

Everything up to a live connection (`assert_paper_only`, `build_config`, `build_strategies`, the
instrument/bar-type/venue correctness above) has real test coverage (`tests/test_node_live.py`)
and needs no live connection to verify — the mapping fixes themselves were confirmed via a
read-only `reqContractDetails` probe against the live TWS paper session, not guessed.

### Phase N7 — CLI, journal, reflection, dream, alerts

**Owed from N2 (audit finding F2):** `Technicals.rsi` is `nan` on a zero-loss window (constant/
monotonic tape, inherited verbatim from the MT5 build, where it was never actually written to
JSON). `journal.py` writes `QuantAnalystReport.to_dict()` straight to JSONL — a bare `NaN` isn't
valid JSON (RFC 8259), so that record silently breaks any non-Python reader (`jq`, `JSON.parse`).
Clamp `rsi` to a neutral 50.0 when there are no losses in the window before wiring this up.

`journal.py` verbatim (+ `position_id`/`client_order_id` fields). `reflection.py`'s input
changes from MT5 deal-P&L-by-ticket to `self.cache.positions_closed()` keyed by position id —
the aggregation logic (win rate by symbol/regime/vol-regime/conviction) is unchanged.
**`dream.py`'s state layer already landed in N2** (`wit/ops/dream.py`: `DreamState`/`Lesson`/
`LessonScore`/`load`/`save`, directly tested in `tests/test_dream_state.py`) — what's left here
is the orchestration half only: `run()` (the weekly LLM call, wired to `Reflection`/`Journal`)
and `format_digest()`. `alerts.py` verbatim. CLI: `doctor` (the direct analogue of the MT5
build's — connectivity, all-watchlist instrument resolution, one LLM round-trip, kill-switch
state), `backtest`/`sweep`, `paper`/`live` (`live` requires an explicit `--i-know` flag),
`halt`/`resume`/`status`/`reconcile`.

**As first shipped, then broken by its own headline deliverable:** the RSI clamp landed
(`wit/desks/technicals.py`), the three cron-equivalents (daily briefing 00:05 UTC, daily
review 23:55 UTC, weekly dream Sunday 22:30 UTC) went live inside `FundStateActor`, and
`reflection.py`/`dream.py`'s orchestration half landed — but the first cut's P&L join used
Nautilus's `position_id` as a trade key, exactly the way an MT5 broker ticket would have been
used. **The Phase N7 audit (round 7) found this doesn't work at all**, by executing a real
multi-trade `BacktestEngine` run, not by reading the code: under `OmsType.NETTING` — the only
OMS this system runs, hard-coded by the IBKR execution client, and set by every venue in this
test suite — Nautilus derives `position_id` as the constant `f"{instrument_id}-{strategy_id}"`,
not a per-round-trip identifier, and `Cache` evicts a symbol's prior closed position from its
closed-position index the instant that symbol is re-entered. Fourteen real round trips on one
symbol scored either **zero** trades (position open when the timer fired) or **twelve
identical copies of one surviving trade's P&L**, at a fabricated 100% win rate in every bucket
— numbers the dream cycle would have hand to the committee as ground truth about its own edge.
The same query backs `FundStateActor`'s daily-loss breaker, so it inherited the identical
defect: after fourteen closed trades, `_realized_pnl_since` still read 0.0, meaning **the
breaker could not latch under any market condition** — a correctness gap the N6 audit's finding
F3 fix (dropping a stale `venue=` filter) had made non-empty but not complete, since it removed
one reason the query came back empty without touching the other. Fractional Kelly, wired this
same phase, was consequently inert too (an 8-symbol watchlist could never reach
`kelly_min_trades`, since the cache can hold at most one closed position per instrument), and
the only test guarding it asserted `0.0 < kelly_mult` — true of every reachable return value,
so it could not have caught the sample being permanently empty.

**Fixed in the same phase, verified by the same method (execution, not inspection).** The join
no longer uses `position_id` or `client_order_id` at all: `WitStrategy.on_position_closed` now
journals a structured `realized_pnl` field on every real closure (previously only embedded in
a free-text message), and `Reflection.review()` pairs each symbol's executed decisions with its
`position_closed` events **chronologically, per symbol** — since `RiskConfig.per_symbol_max_positions
= 1` guarantees a symbol's trades open and close strictly in sequence, the *n*-th execution and
the *n*-th close are provably the same trade, no id needed. `dream.py`'s `run()` and the CLI's
`review`/`dream` commands no longer take a `cache`/`--pnl-json` argument at all — P&L now comes
straight from the journal, which also fixed a second, independent bug the audit caught in the
same pass: `Reflection.review()` windowed on wall-clock `datetime.now(UTC)` while
`FundStateActor`'s callers passed simulated time everywhere else (the N5 audit's F1/F3 bug
class), so a `days=1` review inside a backtest reported a monotonically growing cumulative
count instead of one simulated day; it now takes the same explicit `now` `dream.run()` already
did. The daily-loss breaker and Kelly sizing were rebuilt on a new `FundStateActor` accumulator
(`record_realized_pnl`) fed directly by `WitStrategy.on_position_closed` in real time — never
evicted, unlike `Cache` — and both are now regression-tested against a real multi-trade
backtest: `_realized_pnl_since(0)` is asserted equal to the journal's own independent sum of
every closed trade, and a second test drives a real realized loss past a (deliberately tiny)
cap and asserts the kill-switch file actually appears on disk. The Kelly test now pins the
observed sample size against the journal's count and requires a non-1.0 multiplier, replacing
the tautological assertion.

Three smaller findings from the same audit, all fixed: the RSI clamp's *value* was wrong-headed
— a monotonically rising tape (no losses, real gains) is Wilder's actual 100.0, not a "neutral"
50.0; only a genuinely flat tape (no gains and no losses) is 50.0, and the pure-downtrend mirror
case (already correct) is 0.0, now pinned by tests for all three. `FundStateActorConfig.dream_state_path`
carried a `""` default while `kill_switch_file` deliberately has none (N5 F4) — closing that
gap for kill switches but not dream state meant a caller who omitted the field (the field's
default exists specifically to allow that) would still silently write the real production
`data/dream_state.json`; `dream_state_path` now has no default either, matching
`kill_switch_file` exactly. `FundStateActor.on_stop` manually canceled each timer by name, which
is not only redundant (`Actor._stop()` already calls `self._clock.cancel_timers()`
unconditionally right after `on_stop()`, confirmed against the installed
`nautilus_trader/common/actor.pyx`) but strictly worse: `cancel_timer(name)` raises `KeyError`
on an unregistered name, so a failure partway through `on_start`'s four `set_timer` calls would
have had its real error masked by a `KeyError` during shutdown. Deleted.

`market_intel` is wired into `WitStrategy._on_bar_work` behind a new
`WitStrategyConfig.enable_market_intel` flag, default `False` — a backtest must never depend on
a live yfinance/Finnhub call for determinism (the MT5 build had the same split:
`orchestrator.py`'s live cycle called it, `backtest.py` never imported it at all);
`node_live.py`'s `build_strategies()` turns it on for live/paper only. Also fixed:
`build_node()`'s `FundStateActor` construction referenced `CONFIG.dream_state_path`, which
doesn't exist on `Config` (only `CONFIG.dream.state_path`) — an `AttributeError` on every real
boot, caught by actually calling `build_node()` with fake env vars rather than trusting the
existing tests, none of which exercise that function. `alerts.py` ported verbatim, with one
widened `except` clause (`http.client.HTTPException`, alongside the existing `URLError`/
`TimeoutError`/`OSError`) so the module docstring's "every send failure is swallowed" claim is
actually true.

**CLI, as shipped — narrower than specced, deliberately, and simpler than the first cut
because of the audit.** `doctor` gained a real LLM round-trip (mirrors the MT5 build's) plus
kill-switch state; `halt`/`resume`/`status` operate the kill-switch file directly (the same
file `FundStateActor` polls); `review` and `dream` fire `Reflection`/`dream.run()` against the
journal with **no external P&L argument at all** — the first cut's `--pnl-json` flag existed
only because the broken `position_id`-based join needed an outside P&L source; the journal-only
redesign needs nothing external, so the flag was deleted rather than fixed. `wit dream` also
gained a `--state-path` (defaulting to a scratch file, never `data/dream_state.json`) and a
caught `LiveCommitteeProvider()` construction failure — the audit found it previously crashed
on a raw traceback when `ANTHROPIC_API_KEY` was unset, the opposite posture of its sibling
`doctor`, and could silently overwrite the real production lessons file from a throwaway manual
run. `paper`/`live` boot `node_live.run()`, `live` gated by `--i-know`. **Not built this phase:
`backtest`/`sweep`, and a live-connected `doctor`/`reconcile`.** The MT5 CLI's `backtest`
pulled bars from `MT5Adapter.candles()`; wit-nautilus has no equivalent historical-bars source
yet (no data-fetch subsystem exists — this is new infrastructure, not a port, comparable in
scope to its own phase, not a CLI afterthought). A live IB connectivity/instrument-resolution
check inside `doctor`, and a broker-side `reconcile`, both need an actual TWS/Gateway
connection to mean anything — the same "read-only introspection is not operation" boundary the
N6 audit drew, but connectivity/instrument-resolution *is* read-only and safe to build; it was
deferred here for a narrower reason: getting the async connect/disconnect lifecycle right
without a live session to verify against risks shipping the exact kind of unverified,
confidently-committed live-path code the N6 audit criticized. Nautilus's own exec-engine
reconciliation already runs automatically on every `node.run()` connect
(`LiveExecEngineConfig(reconciliation=True)`, confirmed in N6). Both remain Phase N9's attended
gate, the same way N6 itself deferred `node.run()` rather than guess at it unverified.

**Round 8 (verification audit) — the chronological join wasn't the fix either, and neither was
`now=`.** A follow-up audit round, run specifically to verify the C1/C2 fixes above rather than
to hunt fresh ground, confirmed C1 and C2 themselves are genuinely fixed (re-derived by
executing a fresh 400-bar `BacktestEngine` run independently of this repo's own test suite:
`Cache.positions_closed()` held zero of 22 real round trips, `FundStateActor._closed_pnls` held
all 22, and `_realized_pnl_since` matched the journal's own sum exactly) — but found two of the
fixes built on top of that redesign didn't hold:

- **The `Reflection.review(now=...)` fix from earlier in this phase never mattered.**
  `Journal.write` still stamped every record's `ts` with real wall-clock `datetime.now(UTC)`
  regardless of what `now` a caller passed to `review()` — so `entries_since()`'s window filter
  was comparing a simulated cutoff against wall-clock-stamped entries, the same "everything
  written in the last few seconds of real time" degeneration the original H1 fix claimed to
  close. Reproduced directly: a nominal `days=1` review inside a multi-day backtest still
  reported a monotonically growing decision count. Fixed properly this time by giving
  `Journal.log_decision`/`log_event` a `ts=` parameter and threading `self.clock.utc_now()` (or,
  for fill/close events, the event's own `ts_event`/`ts_closed`) through every call site in
  `WitStrategy`/`FundStateActor`/`dream.run()` — `Journal.write`'s wall-clock stamp is now only
  the fallback for a caller with no clock of its own.
- **The chronological-per-symbol FIFO join (this phase's first fix for C1) was still wrong.**
  `order.ok=True` on a journalled decision means the order was *submitted*, not that it *filled*
  — a rejected or cancelled order has no `position_closed` event, so FIFO silently consumed the
  next real close on that symbol against the decision that never actually traded, permanently
  offsetting every later pairing on that symbol by one slot. The same FIFO also mis-attributed a
  trade whose entry or close straddled a review window boundary, and — unlike the doc note this
  replaced claimed — that skew does *not* self-correct on the next call, since every subsequent
  pairing on the affected symbol inherits the same one-slot offset. Both reproduced directly
  against real journal data.

  **Fixed by dropping the chronological assumption entirely** in favor of an exact id match:
  `PositionClosed.opening_order_id` is a real `ClientOrderId` (confirmed against the installed
  `nautilus_trader/model/events/position.pyx`, not guessed) — the *same* id `log_decision`
  already journals as a completed entry's own `client_order_id`. `WitStrategy.on_position_closed`
  now journals `opening_order_id` alongside `realized_pnl`, and `Reflection.review()` joins on
  `client_order_id == opening_order_id` directly: a rejected order's id never appears as an
  `opening_order_id` anywhere, so it's excluded by construction rather than stealing another
  trade's slot, and a trade whose entry or close falls outside the review window is excluded on
  its own with zero effect on any other trade's pairing. This is also strictly simpler than the
  FIFO it replaced — no per-symbol cursor, no chronological-order assumption to state or defend.

  One more gap the same round found and this fix also closes: `FundStateActor._closed_pnls`
  started empty on every construction, so a process restart mid-day left the daily-loss breaker
  blind to every trade that closed before the restart — real loss from before and after a
  restart could sum to roughly double `max_daily_loss` before the breaker could ever latch.
  `on_start()` now calls a new `_rehydrate_closed_pnls()` that recovers recent closures from the
  journal itself (which already durably records every one) before the actor does anything else —
  correct only because journal entries now carry the actor's own clock, not wall-clock time, so
  this fix depends on the H1 fix above rather than being independent of it. All three are now
  regression-tested against real multi-trade/multi-restart backtests, not unit-level fakes:
  journal timestamps land in the simulated bar period (2026-01, never real wall-clock "today");
  a rejected order provably doesn't offset a later trade; a close whose entry falls outside the
  window is provably excluded rather than misattributed; and a second, freshly-constructed
  `FundStateActor` against the same journal file recovers the first run's full trade history.

**Known, accepted residual (non-blocking, noted for N8):** `Journal.log_decision`'s `cycle_id`
field is still never populated by any caller — dead code, unrelated to either join redesign
above. Two runs writing to the same journal could still produce colliding `client_order_id`/
`opening_order_id` values (Nautilus derives them from simulated date + a per-strategy counter
that restarts at 1 for a fresh strategy instance) — this doesn't corrupt `_closed_pnls` (which
sums every `position_closed` event unconditionally, id or no id) but could make `Reflection`'s
dict-keyed join silently prefer one run's trade over the other's for a colliding id. Every
current call site already gives each backtest its own journal path by construction, so this is
unreached today — Phase N8's sweep harness should either keep that convention explicitly or
populate `cycle_id` and filter on it.

### Phase N8 — Docker / Compose on the VPS

Docker Engine + Compose plugin, `ufw` allowing only 22, non-root user in the docker group (SSH
key-only is already in place).

`ib-gateway` service (`ghcr.io/gnzsnz/ib-gateway:stable`, pinned by digest, `TRADING_MODE=paper`,
VNC bound to `127.0.0.1:5900` for one-time login/2FA over an SSH tunnel, 4002 exposed only on
an internal network) + `fund` service (our image, `depends_on: ib-gateway (service_healthy)`,
no published ports — it only initiates connections). `data/` is the one stateful volume that
matters (journal, dream state, kill switch) — nightly backup. Skip Redis for the cache/msgbus in
the MVP; native exec-engine reconciliation + the journal cover restart continuity. Research
image (`vectorbt`) is a separate Compose profile, never in default `up`.

**Add a staleness watchdog** in `FundStateActor`: if no bar arrives for any subscribed
instrument within 2× the bar interval, alert; escalate to the kill switch after a longer
window. IB's daily gateway restart makes "quietly blind" a real failure mode even with the
resubscription fix.

**As shipped:** `docker/compose.yml` and `docker/Dockerfile` were N1 scaffold placeholders
(explicit "completed in Phase N8" TODOs); both are now real. `ib-gateway` + `fund` on one
user-defined bridge network (`internal` - not Compose's `internal: true` isolation, since
both containers need outbound internet: `fund` for Anthropic/yfinance/Finnhub, `ib-gateway`
for the actual IBKR connection), no API port published to the host either way - `fund`
reaches `ib-gateway` by Compose service name (`IBG_HOST=ib-gateway`, `IBG_PORT=4002`,
overriding `.env.example`'s local-TWS-on-Windows defaults inside the compose file itself).
VNC stays `127.0.0.1:5900` only, reached over an SSH tunnel for the one-time paper-login/
2FA session. The image ships no healthcheck of its own (confirmed against its README, not
assumed) — `ib-gateway`'s is a direct TCP probe of the paper port via bash's `/dev/tcp`;
`fund`'s uses `wit status` rather than `wit doctor` specifically because `doctor` makes a
real Anthropic API call when a key is present, and a healthcheck firing every 60s would
turn that into unbounded, silently-billed background API load. `:stable` floats — pinning
by digest is left as an explicit operator step (the exact `docker inspect` command is in
`compose.yml`'s own comments) rather than a digest hardcoded here that this session has no
way to verify is current or correct. `docker/vps-setup.sh` (Docker Engine + Compose plugin,
`ufw` allowing only 22, the deploy user added to the `docker` group) and
`docker/backup-data.sh` (nightly `data/` tarball, 30-day retention, meant for cron on the
host rather than inside a container so it survives any single container restart) are new -
the build plan named both but neither existed yet. Research image
(`docker/Dockerfile.research`, `vectorbt`/Jupyter) is its own Compose profile, mounts
`data/` read-only (research must never write into the live journal), and is never part of
`docker compose up`'s default target.

The staleness watchdog lives in `FundStateActor._check_bar_staleness()`, polled alongside
the existing kill-switch/multiplier checks rather than on its own timer. It reads
`Cache.bar()` directly — not through any `WitStrategy` — so it has no dependency on
strategy internals and, notably, no exposure to the NETTING `position_id` issue N7's audit
spent two rounds on (bars aren't positions; `Cache` doesn't evict them). `watched_bar_types`
is a tuple of bar-type strings on `FundStateActorConfig` (a plain, serializable field,
unlike `journal`/`committee`/`alerter`) — empty by default, so every existing backtest/sweep
is unaffected; `node_live.py`'s new `watched_bar_types()` populates it for the real node,
sharing the exact bar-type-string construction `build_strategies()` uses (a dedicated
`_bar_type_str()` helper, factored out specifically so this doesn't become a second copy of
the string-building logic N6's audit finding F1 already burned once). Two thresholds, both
multiples of the bar interval: past `stale_alert_multiplier` (default 2×) it alerts once, on
the transition into staleness, not every 30-second poll; past `stale_halt_multiplier`
(default 6×) it also engages the kill switch. Verified against a real `BacktestEngine` run,
not a mock: bars for a symbol stop mid-backtest while a *different* Nautilus data type
(quote ticks) keeps the engine's simulated clock — and the poll timer — advancing past that
point (confirmed necessary: `BacktestEngine.run()`'s own docs state "timer advancement stops
at data exhaustion", so a timer cannot outlive the run's very last event on its own).

**The Phase N8 audit (round 9, against commit `204124e`) found the as-shipped watchdog was
session-blind, and the deployment plumbing around it had two networking defects — verdict
FAIL, 1 Critical/2 High/4 Medium/4 Low/3 Informational.** `_check_bar_staleness()` measured
raw bar age against a fixed multiple of the interval with no concept of market hours (C1):
every RTH-only equity watchlist would halt itself every single night, and every FX symbol
every weekend, the exact failure mode the same finding warned about at design time above —
proven again by executing a real backtest across an overnight equity close and a weekend FX
gap, not by inspection. A permanently-missing first bar on a subscription (`Cache.bar()`
returning `None` forever) was separately invisible to the watchdog (H2): treated as
"still warming up," it never accumulated age, so the one failure mode the watchdog exists to
catch — subscribed but silently never receiving data — was the one case it could not detect.
On the deployment side, `IBG_PORT=4002` (H1) targeted a port the `gnzsnz/ib-gateway` Docker
image binds to its own container's loopback only; the image republishes the real paper port
via `socat` on `4004`, so `fund` could never actually reach `ib-gateway` inside Compose, and
`node_live.py`'s `PAPER_PORTS` allowlist would have rejected that same port as non-paper had
the networking worked at all. Medium findings: no liveness signal existed for the Docker
`HEALTHCHECK` (`wit status` always exits 0 regardless of whether the poll loop is alive or
wedged); the image hardcoded `useradd --uid 1000`, silently unable to write the bind-mounted
`data/` — including the kill switch — on any VPS where the deploy user's real uid differs.
Lower-severity findings: `vps-setup.sh` hardcoded `ufw allow 22/tcp` (would lock out a VPS
already hardened to a non-standard SSH port) and checked `command -v docker` alone to decide
whether to (re)run the installer (a Compose-less Docker CLI would pass that check and then
fail on the script's own last line); `backup-data.sh` didn't tolerate tar's exit code 1
(triggered by the live-growing journal changing size mid-read), aborting the nightly cron
before its retention prune ran; a negative bar/session age (clock skew) would have silently
read as "healthy" forever; and the build context (repo root) shipped `.venv/` (~890MB) and
`.git/` to the Docker daemon on every build with no `.dockerignore`.

**Fixed in the same phase, re-verified by full suite + lint, not by re-reading the diff.**
`wit/ops/market_hours.py` gained `is_session_open()` — unlike the pre-existing
`is_tradeable()` (left untouched; still equity-only for its existing callers), it covers
every watchlist symbol, deferring to `is_tradeable()` for configured equities and applying a
standalone Friday-17:00-to-Sunday-17:00-NY FX week-close rule otherwise. `_check_bar_staleness()`
was rewritten around a single new `_quiet_since` mechanism that closes C1 and H2 together: a
per-symbol timestamp reset to *now* the moment that symbol's session is observed to transition
closed → open, used as the staleness reference whenever no bar exists yet (fixing H2 — a
missing first bar now accumulates real age from session-open instead of never aging) and as a
floor under the bar-timestamp reference otherwise (fixing C1 — a closed session is skipped
entirely, and the first poll after reopen measures age from reopen, not from the stale
pre-close bar, avoiding a false-positive at every single market open). `docker/compose.yml`'s
`IBG_PORT` moved to `4004` and `ib-gateway`'s healthcheck now probes `4004` directly (the exact
path `fund` uses) rather than the unreachable `4002`; `node_live.py`'s `PAPER_PORTS` grew to
accept `4004` alongside the native 7497/4002. A new `wit healthcheck` CLI command (`wit/cli.py`)
reads a heartbeat file `FundStateActor` now touches every poll (`heartbeat_path` on
`FundStateActorConfig`, wired by `node_live.py`'s `build_node()`), failing if it's missing or
stale by more than 300s — `docker/Dockerfile`'s `HEALTHCHECK` now runs this instead of `wit
status`, a real liveness signal with no Anthropic API call in the loop (`wit doctor` was
rejected for the same reason the design section above already rejected it). The Dockerfile
also gained `ARG FUND_UID=1000`, threaded into `useradd --uid ${FUND_UID}`, with
`compose.yml`'s `fund` service passing it through `build.args` from a `FUND_UID` env var the
deploy documentation now calls out explicitly. `vps-setup.sh` detects the real configured SSH
port (`sshd -T`, falling back to grepping `sshd_config`, falling back to 22) before writing its
one `ufw allow` rule, and probes `docker compose version` independently of `command -v docker`
before deciding whether to (re)run the installer. `backup-data.sh` now treats tar's exit code 1
as a warning, not a fatal error, so the retention prune still runs after a backup that raced a
live-growing journal write. `_check_bar_staleness()` also now clamps and logs (rather than
silently accepts) a negative age. A root `.dockerignore` excludes `.venv/`, `.git/`, and the
`data/`/`backups/` bind-mount targets from the build context. Two new secrets files split per
service (`docker/ib-gateway.env.example` for the third-party image, `.env.example` trimmed to
`wit`'s own vars) close a lower-severity finding that a single shared `.env` gave the
third-party `ib-gateway` image blanket access to `wit`'s own secrets and vice versa. Full suite
(265 tests) and `ruff check .` both clean after the fixes, including new/updated coverage for
session-gated staleness across an equity close and an FX weekend, a permanently-missing first
bar, the alert/halt middle band, and the dockerized paper-port allowlist.

### Phase N9 — Validation gate

Mirrors the MT5 build's `doctor` → `once` → `once --execute` → `schedule --execute` discipline,
with soaks for the new failure modes, each bar numeric not a feeling:

1. `pytest` on the VPS, full suite green (no IB involved).
2. `wit doctor` — gateway connects, `DU`-prefixed account, all watchlist instruments resolve,
   one LLM round-trip from the VPS's IP.
3. `wit backtest` over ≥6 months with `ReplayCommitteeProvider` — first honest P&L read.
4. **48-hour data soak**, submission disabled: ≥99% expected bars/instrument, no gap >2
   consecutive bars, survives the daily gateway restart with subscriptions intact, zero
   unexplained container restarts. Compare `market_intel` error rate to the Windows baseline
   (datacenter IPs get rate-limited harder by yfinance than residential ones).
5. **24-hour full dry run**, submission still disabled: every bar journals, both daily timers
   fire, zero unhandled errors, Telegram alerts arrive.
6. **First paper order**, one low-risk instrument, watched — verified via journal, `wit status`,
   and IB's own paper account UI.
7. **Prove the brakes**: `wit halt` blocks the next bar's submission; force the daily-loss
   breaker with a temporarily lowered cap and confirm it latches. A kill switch never pulled is
   not a kill switch.
8. Full watchlist on paper for **≥2 weeks** before "go live" is even discussed as its own,
   separate decision.

**Single-executor rule carries over**: the Windows MT5 build and this VPS build have separate
journals and separate broker accounts, so they're genuinely independent — but never run two
`wit live` instances against one IB account.

### Phase N10 — Alpaca adapter (deferred; scope only after N9 passes)

RFC #3374 is open, unassigned, no branches/PRs. **Material finding that reshapes this phase:**
NautilusTrader's current adapter developer guide says adapters are **Rust-native** — data/exec
client traits implemented in Rust, exposed to Python via PyO3, with a `crates/adapters/<name>/`
layout. There's no Python skeleton documented for a from-scratch adapter, even though older
Python-base-class adapters (`LiveMarketDataClient`/`LiveExecutionClient`) still visibly exist.

Two genuinely different projects, pick before writing code:

| | A. Private Python adapter | B. Upstreamable Rust crate |
|---|---|---|
| Effort | Weeks | Months + Rust competence |
| Upstreamable | No | Yes |
| Risk | Python base classes may be a legacy path with no forward guarantee | Larger, but aligned with the project's direction |

**Recommendation: A, private and out-of-tree — but comment on RFC #3374 first** asking
maintainers directly whether a Python-side adapter is still viable out-of-tree. Costs nothing,
resolves the one question docs can't answer, and stakes a claim if upstreaming is wanted later
(which converts a private integration into an ongoing public maintenance commitment — flag it
as an option, don't default into it).

Sub-phases once unblocked: paper-first (Alpaca paper is a separate base URL/keys, mirror the
`paper_only` boot assertion) → `AlpacaInstrumentProvider` (US equities first; crypto/options
later) → `AlpacaDataClient` (REST bootstrap + WS stream, in the connection order the adapter
guide mandates) → `AlpacaExecutionClient` (order lifecycle + bracket support) → config/factory
classes → mock-server tests → Alpaca paper → the same N9 gate verbatim. **The strategy code
must not change at all** — if it does, the adapter boundary leaked.

---

## 4. Open questions — unconfirmed from docs, not guessed

**Update (N0, static inspection against `nautilus_trader==1.230.0`, pinned via
`pip install nautilus_trader` — no live gateway needed for these):**

1. **RESOLVED.** `OrderFactory.bracket()`'s full signature (`nautilus_trader.common.factories`):
   ```
   bracket(self, instrument_id, order_side, quantity, quote_quantity=False,
     emulation_trigger=NO_TRIGGER, trigger_instrument_id=None, contingency_type=OUO,
     entry_order_type=MARKET, entry_price=None, entry_trigger_price=None, expire_time=None,
     time_in_force=GTC, entry_post_only=False, entry_exec_algorithm_id=None,
     entry_exec_algorithm_params=None, entry_tags=None, entry_client_order_id=None,
     tp_order_type=LIMIT, tp_price=None, tp_trigger_price=None, tp_trigger_type=DEFAULT,
     tp_activation_price=None, tp_trailing_offset=None, tp_trailing_offset_type=PRICE,
     tp_limit_offset=None, tp_time_in_force=GTC, tp_post_only=True, tp_exec_algorithm_id=None,
     tp_exec_algorithm_params=None, tp_tags=None, tp_client_order_id=None,
     sl_order_type=STOP_MARKET, sl_trigger_price=None, sl_trigger_type=DEFAULT,
     sl_activation_price=None, sl_trailing_offset=None, sl_trailing_offset_type=PRICE,
     sl_time_in_force=GTC, sl_exec_algorithm_id=None, sl_exec_algorithm_params=None,
     sl_tags=None, sl_client_order_id=None)
   ```
   Default `contingency_type=OUO` (one-updates-other) matches the plan's intent — entry fills,
   SL/TP become live; one fills, the other cancels. N5's `_on_decision` call is:
   `self.order_factory.bracket(instrument_id=.., order_side=.., quantity=.., sl_trigger_price=.., tp_price=..)`.
2. **Still open.** `Strategy`/`Actor` expose no `risk`-named or `trading_state`-named method
   directly (confirmed by attribute scan — empty result). `RiskEngine.set_trading_state` exists
   on the engine itself; the path from strategy code to it (message-bus command vs. a kernel
   handle) is still unconfirmed. Unchanged conclusion: doesn't block N5, since the kill switch's
   primary enforcement is our own pre-submit gate, which needs no framework API.
3. **RESOLVED.** `Trader.add_actor(self, actor: Actor) -> None` — single positional arg, exactly
   as assumed.
4. **RESOLVED.** Full accessor set confirmed. `Portfolio` (`nautilus_trader.portfolio.portfolio`):
   `account(venue)`, `equity(venue)`, `unrealized_pnl`, `realized_pnl`, `total_pnl`,
   `margins_init`, `margins_maint`, `balances_locked`. The `Account` object itself
   (`nautilus_trader.accounting.accounts.base.Account`) has the free-margin accessors that
   weren't confirmed before: **`balance_total`, `balance_free`, `balance_locked`**, plus
   `balances_total`/`balances_free`/`balances_locked` (multi-currency), `id` (an `AccountId` —
   need to confirm its string form carries the `DU…` prefix for the paper_only assertion, N6),
   `is_margin_account`. The `AccountInfo` shim in N4 maps directly to these.
5. **RESOLVED.** `request_bars` docstring, confirmed verbatim: "Once the response is received,
   the bar data is forwarded from the message bus to the `on_historical_data` handler." Exactly
   as the plan assumed — no completion-signal gate exists natively, so N5's `ready` flag
   (flipped once `warmup_bars` accumulate in `on_historical_data`) is still the right design.
6. **RESOLVED — the most important one.** Both `Actor` and `Strategy` expose
   `run_in_executor(func, args=None, kwargs=None) -> TaskId` and
   `queue_for_executor(func, args=None, kwargs=None)` (sequential variant), backed by
   `register_executor(loop, executor)`. Docstring, verbatim: **"For backtesting the `func` is
   immediately executed, as there's no need for a `Future` object that can be awaited... the
   results of `func` are 'immediately' available after it's called."** This is exactly the
   `DecisionProvider` off-loop mechanism the plan needed, and it resolves the live/backtest
   duality *for free* at the framework level — in backtest, `run_in_executor` degrades to a
   direct synchronous call (fine, since `ReplayCommitteeProvider`'s cache lookup is already
   synchronous and cheap); in live/paper, it genuinely schedules onto a registered executor off
   the event-loop thread. N5's `on_bar` calls
   `self.run_in_executor(self._deliberate_and_decide, args=(report,))`; `_deliberate_and_decide`
   itself calls the (still separately async, for the rate limiter's sake)
   `DecisionProvider.deliberate_async` and then does the sizing/order/journal sequence. Still
   need to confirm in N0 live-connectivity testing: what executor `register_executor` should be
   given in `node_live.py` (a `ThreadPoolExecutor` sized to the committee's concurrency needs is
   the working assumption — ties into open question 11's staggered-warmup finding too).
7. **`set_time_alert` semantics parity** between `BacktestEngine`'s simulated clock and
   `TradingNode`'s live clock — determines whether the dream cycle is backtestable.
8. **RESOLVED (live probe against TWS paper, `127.0.0.1:7497`, account `DUR305728`).** Login +
   2FA already cleared by the user manually (this is inherently a human step — TWS was already
   up and authenticated when probed). Confirmed via `reqAccountSummary`: `AccountType=INDIVIDUAL`,
   `NetLiquidation=$1,000,000`, `BuyingPower=$4,000,000` — a real, funded paper account.
   **New finding, not originally asked:** TWS's API is disabled by default even when TWS itself
   is running — "Enable ActiveX and Socket Clients" must be checked manually in
   Global Configuration → API → Settings before anything connects. Worth a callout in N6/N8's
   runbook since it's an easy thing to forget on a fresh install.
9. **PARTIALLY RESOLVED, and this is an action item for the account, not code.** Live-probed
   both `reqMarketDataType(1)` (live) and `reqMarketDataType(3)` (delayed) against NVDA
   (`SMART`/`NASDAQ`): **both fail with error 10089** ("Requested market data requires
   additional subscription for API"). This paper account currently has **no US equities market
   data entitlement at all**, not even the free 15-minute-delayed tier — that needs to be
   enabled once in IBKR Account Management (Market Data Subscriptions), separate from anything
   this codebase can do. **FX is unaffected**: `EURUSD` on `IDEALPRO` returned live ticks
   immediately with zero subscription needed (bid/ask/close all populated) — IDEALPRO spot FX
   data has always been free on IBKR, this confirms it still is. **Action for the user, before
   N6**: enable US equities market data (the free delayed tier is enough for N9's early gates;
   real-time is a paid add-on, cents/month, needed before live). Until then, `wit doctor`
   should treat the equity leg of the watchlist as blocked and say so plainly rather than fail
   opaquely.
10. **RESOLVED for the confirmed watchlist** (live `reqContractDetails` probe, all 8 symbols
    resolved cleanly): all 7 US equities (`NVDA`/`MSFT`/`AAPL`/`AMZN`/`GOOGL`/`META`/`TSLA`,
    `STK`/`SMART`/`NASDAQ`) came back with `minTick=0.01`, `priceMagnifier=1` — confirms §1.3's
    "US equities: 1 share = 1 unit, trivial" assumption exactly, no surprises. `EURUSD`
    (`CASH`/`IDEALPRO`) resolved with `minTick=0.00005` (half-pip, 5-decimal quoting) — also
    straightforward, not a blocker for the sizing shim. No futures/options in this watchlist, so
    the harder `multiplier`-based case in §1.3 stays theoretical for now, as planned.
11. **Partially resolved — single-request case is fast and clean, concurrency untested.** A
    single-symbol `reqHistoricalData` (NVDA, 2 weeks of 15-min bars = 260 bars) completed in
    **1.3 seconds**, no pacing violation, no error. This is a good sign but does **not** clear
    the concurrent-warmup question — issue #3718's failure mode is specifically about *multiple
    simultaneous* historical requests across strategy instances, which this single-symbol probe
    doesn't exercise. Still needs a real 10-symbol concurrent test once N5's strategy code
    exists to drive it (staggering is cheap insurance either way).
12. **Whether the daily-restart resubscription fix (issue #3733, closed) is in the pinned
    version** — don't assume, verify.
13. **IB's minimum stop distance behavior** — no `stops_level_points` equivalent found; the
    configured floor in §1.3 is a substitute, not a confirmed translation.
14. **Whether a pure-Python NautilusTrader adapter is still viable out-of-tree** — the question
    to put to RFC #3374 before N10 picks A or B.
15. **vectorbt's maintenance trajectory** — open-source still publishes but development has
    shifted to a paid fork; this is exactly why §2 keeps it research-only with no live-path
    dependency.

---

## Risks

| Risk | Mitigation |
|---|---|
| **CONFIRMED (N0, live):** this paper account has no US equities market data entitlement (error 10089 on both live and delayed) | User enables it once in IBKR Account Management before N6; `wit doctor` should detect and report this per-instrument-class rather than fail generically. FX is unaffected — confirmed working with zero subscription |
| Committee latency blocks the event loop → missed fills/stale data | Off-loop `DecisionProvider` design; async rate limiter, never `time.sleep`; N0 confirms the mechanism before N5 |
| `tick_value` shim wrong → position sizes off by an order of magnitude | N0's real instrument table; N4's gate is the MT5 sizing suite green on the new spec; first paper order on one instrument, watched |
| **CONFIRMED (N4 audit):** `InstrumentSpec.value_per_unit` defaults to 1.0, which is silently wrong (understates risk, over-sizes) for a futures-resolved instrument (multiplier != 1) or a non-USD-quote instrument | Watchlist trimmed to the N0-confirmed 8 equities+EURUSD (no metals/index) so 1.0 is correct for everything currently traded; any future addition of a futures/FX-cross instrument must pass an explicit `value_per_unit` at every `spec_for()` call site, not rely on the default — see `instrument_spec.py`'s docstring |
| `InstrumentSpec.min_stop_distance` defaults to 0.0 (no floor) per instrument — MT5's broker-reported `stops_level_points` had this for free | Must be set per instrument before N6 goes live on real orders; `wit doctor` (N7) should warn when any traded instrument is still at 0.0 |
| A malformed/crossed live quote (`last_close <= 0`, negative `ask - bid`) reaches `build_plan`/`revalidate_plan` | **CONFIRMED (N4 audit) and fixed:** both functions now block on `last_close <= 0` or a negative `spread` with a dedicated reason string, restoring the fail-closed behavior MT5's non-negative broker-reported spread int provided for free |
| Silent divergence between old/new desks | N2's byte-identical `as_prompt_block()` fixture gate |
| IB gateway daily restart leaves the engine blind | `FundStateActor` staleness watchdog; 48h soak spans ≥2 restarts |
| IB pacing violation disables the API session mid-run | Serialized/staggered warmup, measured in N0 before it's a production surprise |
| Paper 2FA makes unattended restart impossible | N0 item 8, answered before any VPS work |
| Two executors against one account | Separate accounts by construction (MT5 demo vs IB paper); single-executor rule in the README |
| yfinance rate-limits the datacenter IP → committee loses market intel silently | N9.4 compares intel error rate to Windows baseline; Finnhub covers equities as a fallback |
| LLM gateway silently substitutes models | Default to direct Anthropic; `served_model` logged regardless |
| Research deps (numba/numpy pins) leak into the live image | Separate Compose profile/image target; vectorbt never in the live container |

## Explicitly not doing

- No `BrokerAdapter` ABC in the new repo — Nautilus's client traits are the abstraction already.
- No APScheduler — the Nautilus clock covers bar cadence and the non-bar cron jobs, in backtest too.
- No relaxation of any risk cap, gate, threshold, or gate ordering — the port is mechanical.
- No Redis/Postgres cache backing in the MVP.
- No changes to `c:\Users\fredd\Projects\SAAS\Wit-Hedge-fund` — read-only reference only.
- No live-money trading in any phase here — "go live" is its own decision after ≥2 clean weeks on paper.
- No Alpaca work of any kind until N9 passes.
- No options, futures, or crypto in the first watchlist — US equities + major FX only, the two
  classes §1.3's sizing math can be confident about (metals/index may need their own phase later).

---

## Critical files (new repo, build order)

- `wit/nautilus/strategy.py` — `WitStrategy`: the whole `Orchestrator.process_symbol` sequence
  re-expressed as `on_start`/`on_bar`/`on_historical_data`/`_on_decision`/`on_order_filled`.
  Highest-risk module.
- `wit/nautilus/actor.py` — `FundStateActor`: kill switch, daily-loss breaker, adaptive
  multipliers, the three cron-equivalents.
- `wit/risk/instrument_spec.py` — the `Instrument` → sizing-spec shim; the single point where a
  mistake becomes wrong position sizes.
- `wit/committee/provider.py` (+ `live.py`/`replay.py`) — the boundary that makes one strategy
  runnable in backtest, paper, and live.
- `wit/nautilus/node_live.py` — `TradingNode` assembly, IB client configs, the `paper_only`
  boot assertion.

Read-only reference in the MT5 repo (do not modify):
- `engine/orchestrator.py` — the sequence being ported
- `engine/risk/sizing.py` — the gates that must survive unchanged
- `engine/safety.py` — the guarantees needing a new home
- `engine/broker/base.py` — the 8 methods being mapped away
- `docs/LINUX_DEPLOYMENT_PLAN.md` — the validation-gate discipline N9 mirrors

## Verification (end to end)

1. `pytest` green in the new repo at every phase gate listed above (N2 desk-equivalence, N4
   sizing-suite, N5 backtest, N9 full soak sequence).
2. `wit doctor` passing from the VPS is the connectivity/config proof point before any live
   bars or orders are attempted.
3. The N9 staged rollout (backtest → 48h data soak → 24h dry run → first watched paper order →
   brakes proven → 2-week full-watchlist paper run) is the actual go/no-go gate — no step skipped,
   each with a numeric bar, before "go live" is even raised as a question.

---

Sources: [Strategies](https://nautilustrader.io/docs/latest/concepts/strategies/) ·
[Actors](https://nautilustrader.io/docs/latest/concepts/actors/) ·
[Instruments](https://nautilustrader.io/docs/latest/concepts/instruments/) ·
[Data](https://nautilustrader.io/docs/latest/concepts/data/) ·
[Cache](https://nautilustrader.io/docs/latest/concepts/cache/) ·
[Portfolio](https://nautilustrader.io/docs/latest/concepts/portfolio/) ·
[Live trading](https://nautilustrader.io/docs/latest/concepts/live/) ·
[Configure a live trading node](https://nautilustrader.io/docs/latest/how_to/configure_live_trading/) ·
[Backtesting](https://nautilustrader.io/docs/latest/concepts/backtesting/) ·
[Risk API reference](https://nautilustrader.io/docs/latest/api_reference/risk/) ·
[IB adapter](https://nautilustrader.io/docs/latest/integrations/ib/) ·
[Adapter developer guide](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/developer_guide/adapters.md) ·
[RFC #3374 Alpaca](https://github.com/nautechsystems/nautilus_trader/issues/3374) ·
[Issue #3733](https://github.com/nautechsystems/nautilus_trader/issues/3733) ·
[Issue #3718](https://github.com/nautechsystems/nautilus_trader/issues/3718) ·
[gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker) ·
[vectorbt](https://github.com/polakowo/vectorbt)
