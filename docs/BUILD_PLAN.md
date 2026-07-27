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

**Gate:** node connects, instruments resolve, warmup+live bars arrive, one bracket order fills
on paper and appears in the journal with a Nautilus position id.

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
