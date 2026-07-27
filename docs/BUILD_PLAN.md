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

**Decision: port `sizing.py`'s logic unchanged**, add a new `instrument_spec.py` shim that
turns a Nautilus `Instrument` into the same `(loss_per_unit, min_qty, qty_step)` shape
`build_plan` already consumes. `build_plan` itself does not change — that's what keeps the risk
guarantees byte-identical. `stops_level_points` (MT5's broker-minimum-stop) has no IB
equivalent; replace with a configured floor, same "widen the stop, don't reject" behavior.

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
3. **Instrument resolution table.** `InteractiveBrokersInstrumentProviderConfig` for the full
   watchlist — record `price_increment`/`min_quantity`/`lot_size`/`multiplier`/`margin_init`
   per symbol. This is the direct input to the §1.3 `tick_value` shim; don't design that shim
   before this table exists.
4. **Historical warmup at scale.** 750×15m bars across 10 instruments at `on_start` — does it
   trip IB's pacing limiter? Does concurrent multi-strategy warmup hit the shared
   historical-request de-dup bug reported in nautechsystems/nautilus_trader#3718?
5. Live bar cadence/latency on `on_bar`.
6. Bracket order behavior (entry+SL+TP as one list) — fill/contingency semantics on partial fill.
7. **Daily gateway restart (~21:00–21:15 UTC) resubscription.** Issue #3733 (closed) fixed this
   for a specific version — confirm the fix is actually in the version being pinned.
8. Clock timer (`set_timer`/`set_time_alert`) parity between live and backtest.
9. Paper market-data subscription behavior — does paper share live's data subscriptions, or is
   `DELAYED_FROZEN` the fallback? MVP should work on delayed data if not.

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

Port `CommitteeDecision` (incl. `.abstain()`, `distinctiveness()`, `model`/`served_model` audit
fields) and the bull/bear/PM prompts verbatim, forced-tool-use schema unchanged. Split the
client into `provider.py` (protocol), `live.py` (async Anthropic + async `_RateLimiter` —
**never a thread-blocking `time.sleep` on the event loop**), `replay.py` (SQLite decision cache
keyed by `(instrument_id, bar_ts_ns, sha256(prompt_block))`, `strict`/`record` modes),
`stub.py` (deterministic, no network).

**Provider decision: default to direct Anthropic, keep `base_url` configurable.** The MT5
build's own notes record its free-tier gateway (NaraRouter) silently serving a different model
than requested — real money reasons to not default a live-order-placing system to a free,
substitution-prone gateway. `served_model` stays logged either way, since it's the only way to
detect substitution if a gateway is used later.

**Gate:** replay `QuantAnalystReport` fixtures from the MT5 repo's journal through
`LiveCommitteeProvider`; confirm every failure mode (timeout, malformed tool call, 429, no key)
returns `abstain` and never raises.

### Phase N4 — Risk/sizing port

1. `adaptive.py` — verbatim (pure math).
2. `instrument_spec.py` (new) — `spec_for(instrument, ...) -> InstrumentSpec` using the Phase
   N0 instrument table.
3. `sizing.py` — port with the *only* substantive change being `SymbolSpec` → `InstrumentSpec`.
   Gate ordering, `MARKOV_VETO_THRESHOLD`, blocked-reason strings, and `revalidate_plan`'s
   checks all stay as-is.

**Gate:** the MT5 repo's `sizing` test suite passes against the new spec type with only fixture
construction changed. Any test that needs rewriting (not just re-fixturing) means a risk
guarantee moved — stop and explain before proceeding.

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

**Gate:** a ≥3-month backtest with `StubPolicyProvider` completes and produces orders/fills/
journal in the same shape as the MT5 build. Then a small-subset run with `ReplayCommitteeProvider`
in `record` mode proves the LLM path end-to-end offline.

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

`journal.py` verbatim (+ `position_id`/`client_order_id` fields). `reflection.py`'s input
changes from MT5 deal-P&L-by-ticket to `self.cache.positions_closed()` keyed by position id —
the aggregation logic (win rate by symbol/regime/vol-regime/conviction) is unchanged.
`dream.py` verbatim, `alerts.py` verbatim. CLI: `doctor` (the direct analogue of the MT5
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
8. **Paper-account 2FA requirement** — determines whether unattended restart is even possible.
   Highest-priority N0 item.
9. **Paper market-data subscription sharing with live** — determines `REALTIME` vs
   `DELAYED_FROZEN` for the MVP.
10. **`tick_value`-equivalent per instrument class**, confirmed only for US equities in theory —
    N0's instrument table must exist before N4's shim is designed.
11. **Concurrent 10-instrument warmup pacing** — issue #3718 (historical-request de-dup) may
    force serialized/staggered warmup.
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
| Committee latency blocks the event loop → missed fills/stale data | Off-loop `DecisionProvider` design; async rate limiter, never `time.sleep`; N0 confirms the mechanism before N5 |
| `tick_value` shim wrong → position sizes off by an order of magnitude | N0's real instrument table; N4's gate is the MT5 sizing suite green on the new spec; first paper order on one instrument, watched |
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
