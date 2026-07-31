"""Assembles a live `TradingNode` against Alpaca (execution) + Polygon (data),
paper-only by construction (build plan §1.4/§3 Phase N6; broker swap keeps this
guarantee verbatim, see ``docs/whatif-we-used-alpaca-quirky-aurora.md``).

**The `paper_only` boot assertion lives here** (§1.4's table names this as the
guarantee's actual home): ``assert_paper_only()`` checks the *configured*
``WIT_PAPER_ONLY`` flag and ``AlpacaConfig.paper`` — both known before any
socket opens — and raises rather than degrading. It runs before
``node.build()``, so a misconfigured live account can never reach the point of
resolving instruments or subscribing to data, let alone submitting an order.
Unlike the IB build's port/account-prefix check, there is no config-only signal
for whether an Alpaca API key is *actually* a paper key (that's only knowable
after an authenticated `get_account()` call, which happens inside
`AlpacaExecutionClient._connect`, after this assertion already ran) — so this
assertion is necessary but not sufficient; the account number's `PA` prefix
was confirmed live in Phase 0 of the swap, and should be watched on every
first connection, not just assumed from config.

**Strategies are assembled manually, not via Nautilus's config-driven
`TradingNodeConfig(strategies=[...])`/`ImportableStrategyConfig` path**
(Phase N5 audit finding F9, unchanged by the broker swap): `WitStrategy.__init__`
takes `provider`/`fund_state` as extra constructor arguments beyond Nautilus's
own `config`-only convention, because those are live Python objects (a shared
rate-limited LLM client, a shared fund-state actor) that don't belong in a
serializable config. `StrategyFactory.create` calls `strategy_cls(config=config)`
and cannot construct this class — so `run()` builds the node with empty
`strategies=[]`/`actors=[]` in its `TradingNodeConfig`, then adds every
instance directly via `node.trader.add_actor()`/`add_strategy()` after
`node.build()`.

**Single venue, not one per exchange** (the broker swap's load-bearing design
decision, verified in Phase 0 against the installed `nautilus_trader`'s
`data/engine.pyx` `register_venue_routing`): every `InstrumentId` here uses
`ALPACA_VENUE`, including the ones whose *bars* come from Polygon. This
actually simplifies `WitStrategyConfig.account_venue` versus the IB build —
IB needed an explicit override (`account_venue=IB_VENUE`) because its account
lived under a different pseudo-venue than any instrument's own SMART/NASDAQ
routing venue; here `instrument_id.venue` already IS `ALPACA_VENUE`, so the
config's own default (`None` → resolved to `instrument_id.venue`) is correct
without an override.
"""
from __future__ import annotations

from pathlib import Path

from nautilus_trader.config import LiveExecEngineConfig, RoutingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import PriceType
from nautilus_trader.model.identifiers import InstrumentId, TraderId

from wit.adapters.alpaca.common import ALPACA_VENUE
from wit.adapters.alpaca.config import AlpacaExecClientConfig, AlpacaInstrumentProviderConfig
from wit.adapters.alpaca.factories import AlpacaLiveExecClientFactory
from wit.adapters.polygon.config import PolygonDataClientConfig
from wit.adapters.polygon.factories import PolygonLiveDataClientFactory
from wit.committee.provider import DecisionProvider, build_committee_provider
from wit.config import CONFIG, AlpacaConfig
from wit.nautilus.actor import FundStateActor, FundStateActorConfig
from wit.nautilus.strategy import WitStrategy, WitStrategyConfig
from wit.ops.alerts import Alerter
from wit.ops.journal import Journal

# Watchlist symbol (the logical, MT5-style form desks/committee/risk key
# everything by) -> (Nautilus InstrumentId string, bar price type). Equities
# only as of the broker swap (EURUSD dropped - Alpaca has no forex, build
# plan's "Architecture" section) - every entry uses the ALPACA venue and
# PriceType.LAST (Alpaca/Polygon both report real trade prints for US equities,
# unlike IB's CASH/FX contracts which needed MID).
INSTRUMENT_IDS: dict[str, tuple[str, PriceType]] = {
    symbol: (f"{symbol}.ALPACA", PriceType.LAST) for symbol in CONFIG.watchlist
}


class PaperOnlyViolation(RuntimeError):
    """Raised by ``assert_paper_only`` when the boot-time paper-only check
    fails. Never caught anywhere in this codebase - the entire point is to
    refuse to start, not to degrade or retry."""


def assert_paper_only(alpaca: AlpacaConfig | None = None) -> None:
    """The paper_only hard lock (build plan §1.4), asserted at boot against
    *configuration* the process already has - no Alpaca connection is opened to
    perform this check, so it can never be bypassed by a slow/failed connection
    falling through to a live default. Necessary but not sufficient: see this
    module's docstring for why the actual paper-account-number confirmation
    can only happen after `AlpacaExecutionClient` authenticates."""
    alpaca = alpaca or CONFIG.alpaca
    if not CONFIG.safety.paper_only:
        raise PaperOnlyViolation(
            "WIT_PAPER_ONLY is not set - refusing to boot a live-capable node. "
            "This is a hard lock and must never be relaxed from .env alone "
            "(build plan §1.4)."
        )
    if not alpaca.paper:
        raise PaperOnlyViolation(
            "ALPACA_PAPER is not set - refusing to boot against Alpaca's live "
            "trading API. This is a hard lock and must never be relaxed from "
            ".env alone (build plan §1.4)."
        )
    if not alpaca.api_key or not alpaca.secret_key:
        raise PaperOnlyViolation(
            "ALPACA_API_KEY/ALPACA_SECRET_KEY are not both set - refusing to boot."
        )


def build_config(alpaca: AlpacaConfig | None = None) -> TradingNodeConfig:
    """The `TradingNode` config. Empty `strategies`/`actors` lists on purpose
    - see this module's docstring for why they're added manually after
    `node.build()` instead.

    Phase N6 audit finding F9 (unchanged by the broker swap): asserts
    paper_only itself too, rather than relying solely on `build_node()`'s
    caller to have checked first - this function returns a fully live-capable
    config given a live `AlpacaConfig`, and it's public."""
    alpaca = alpaca or CONFIG.alpaca
    assert_paper_only(alpaca)

    instrument_ids = frozenset(
        InstrumentId.from_str(pair[0]) for pair in INSTRUMENT_IDS.values()
    )
    provider_cfg = AlpacaInstrumentProviderConfig(
        load_ids=instrument_ids,
        api_key=alpaca.api_key, secret_key=alpaca.secret_key, paper=alpaca.paper,
    )
    exec_cfg = AlpacaExecClientConfig(
        instrument_provider=provider_cfg,
        api_key=alpaca.api_key, secret_key=alpaca.secret_key, paper=alpaca.paper,
    )
    data_cfg = PolygonDataClientConfig(
        instrument_provider=provider_cfg,
        api_key=CONFIG.polygon.api_key,
        max_requests_per_minute=CONFIG.polygon.max_requests_per_minute,
        poll_interval_secs=CONFIG.polygon.poll_interval_secs,
        delayed_minutes=CONFIG.polygon.delayed_minutes,
        alpaca_api_key=alpaca.api_key, alpaca_secret_key=alpaca.secret_key,
        alpaca_paper=alpaca.paper,
        # Polygon's own client venue is None (see PolygonDataClient's docstring);
        # this is what makes the DataEngine send ALPACA-addressed data commands
        # to the "POLYGON" client_id - the broker swap's load-bearing design
        # decision, verified in Phase 0 (data/engine.pyx's register_venue_routing).
        routing=RoutingConfig(venues=frozenset({str(ALPACA_VENUE)})),
    )
    return TradingNodeConfig(
        trader_id=TraderId("WIT-001"),
        exec_engine=LiveExecEngineConfig(reconciliation=True),
        data_clients={"POLYGON": data_cfg},
        exec_clients={"ALPACA": exec_cfg},
    )


def _bar_type_str(instrument_id: str, price_type: PriceType) -> str:
    """The one place a watchlist symbol's instrument id + price type becomes a
    Nautilus bar-type string - shared by `build_strategies()` and
    `watched_bar_types()` so the string is only ever assembled once (IB build's
    Phase N6 audit finding F1 was exactly this string built wrong in two
    slightly different ways in two places; do not reintroduce a second copy)."""
    return f"{instrument_id}-{_bar_step(CONFIG.timeframe)}-{price_type.name}-EXTERNAL"


def watched_bar_types() -> dict[str, str]:
    """Watchlist symbol -> its bar-type string, for
    `FundStateActorConfig.watched_bar_types` (Phase N8's staleness
    watchdog) - computed independently of `build_strategies()` since
    `FundStateActor` is constructed before the strategies are, in
    `build_node()`."""
    return {
        symbol: _bar_type_str(instrument_id, price_type)
        for symbol, (instrument_id, price_type) in INSTRUMENT_IDS.items()
    }


def build_strategies(
    provider: DecisionProvider, fund_state: FundStateActor, journal: Journal | None = None,
) -> list[WitStrategy]:
    """One `WitStrategy` per watchlist symbol."""
    strategies = []
    for symbol in CONFIG.watchlist:
        pair = INSTRUMENT_IDS.get(symbol)
        if pair is None:
            continue
        alpaca_id, price_type = pair
        instrument_id = InstrumentId.from_str(alpaca_id)
        bar_type = BarType.from_str(_bar_type_str(alpaca_id, price_type))
        assert bar_type.instrument_id == instrument_id, (
            f"bar_type instrument mismatch: {bar_type.instrument_id} != {instrument_id}"
        )
        config = WitStrategyConfig(
            instrument_id=instrument_id, bar_type=bar_type, symbol=symbol,
            timeframe=CONFIG.timeframe, history_bars=CONFIG.history_bars,
            # No account_venue override needed (unlike the IB build's IB_VENUE
            # override) - instrument_id.venue is already ALPACA_VENUE, which is
            # WitStrategyConfig's own default resolution. See this module's
            # docstring for why the broker swap simplifies this away.
            enable_market_intel=True,
        )
        strategies.append(WitStrategy(config, provider=provider, fund_state=fund_state,
                                      journal=journal))
    return strategies


# MT5-style timeframe -> (Nautilus BarType step string, seconds). One table,
# not two (IB build's Phase N8 audit finding I3, unchanged by the broker swap):
# _bar_step and bar_interval_seconds used to maintain separate mappings over
# the same five timeframes. Only H1 (CONFIG.timeframe) is exercised currently;
# extend when a per-symbol timeframe is actually needed.
_TIMEFRAMES: dict[str, tuple[str, int]] = {
    "M15": ("15-MINUTE", 900),
    "M30": ("30-MINUTE", 1800),
    "H1": ("1-HOUR", 3600),
    "H4": ("4-HOUR", 14400),
    "D1": ("1-DAY", 86400),
}


def _bar_step(timeframe: str) -> str:
    """MT5-style timeframe ("H1", "M15") -> Nautilus BarType step string
    ("1-HOUR", "15-MINUTE")."""
    if timeframe not in _TIMEFRAMES:
        raise ValueError(f"no Nautilus bar-step mapping for timeframe {timeframe!r}")
    return _TIMEFRAMES[timeframe][0]


def bar_interval_seconds(timeframe: str) -> int:
    """MT5-style timeframe -> seconds, for the staleness watchdog's alert/
    halt thresholds (a multiple of this)."""
    if timeframe not in _TIMEFRAMES:
        raise ValueError(f"no bar-interval mapping for timeframe {timeframe!r}")
    return _TIMEFRAMES[timeframe][1]


def build_node() -> TradingNode:
    """Boot-asserts, builds, and fully wires a `TradingNode` (data/exec client
    factories registered, `node.build()` called) but does NOT call
    `node.run()` - kept separate so tests can exercise everything up to a
    live connection without one. Note `node.build()` itself opens no socket
    (it only constructs the data/exec clients); the actual Alpaca/Polygon
    connections happen later, inside `node.run()`.

    Phase N6 audit finding F7 (unchanged by the broker swap): every fallible
    non-broker object (the committee provider, which raises loudly on missing
    LLM config per Phase N3's design; the fund-state actor; the journal) is
    constructed BEFORE `node.build()`, not after - a missing ANTHROPIC_API_KEY
    must raise before the Alpaca/Polygon clients are already built, leaving
    `node` a local with no `dispose()` call reached. Everything from
    `node.build()` onward is wrapped so a failure disposes the node instead of
    leaking the kernel and the adapters' cached Alpaca client/stream."""
    alpaca = CONFIG.alpaca  # Phase N6 audit finding F10 (unchanged): read once,
    assert_paper_only(alpaca)  # assert, and use this exact object - not re-read
                               # separately by build_config.

    provider = build_committee_provider()
    journal = Journal(CONFIG.journal_path)
    fund_state = FundStateActor(
        FundStateActorConfig(
            venue=ALPACA_VENUE,
            kill_switch_file=CONFIG.safety.kill_switch_file,
            dream_state_path=CONFIG.dream.state_path,
            watched_bar_types=watched_bar_types(),
            bar_interval_seconds=bar_interval_seconds(CONFIG.timeframe),
            heartbeat_path=str(Path(CONFIG.journal_path).parent / "heartbeat"),
        ),
        journal=journal, committee=provider, alerter=Alerter.from_env(),
    )

    config = build_config(alpaca)
    node = TradingNode(config=config)
    try:
        node.add_data_client_factory("POLYGON", PolygonLiveDataClientFactory)
        node.add_exec_client_factory("ALPACA", AlpacaLiveExecClientFactory)
        node.build()

        node.trader.add_actor(fund_state)
        for strategy in build_strategies(provider, fund_state, journal):
            node.trader.add_strategy(strategy)
    except Exception:
        node.dispose()
        raise

    return node


def run() -> None:
    """Boots the live paper-trading node and blocks until stopped
    (Ctrl+C / SIGTERM). See the build plan's Phase N6 gate, re-run for the
    broker swap (Phase 7's staged validation): node connects, instruments
    resolve, warmup+live bars arrive, one bracket order fills on paper and
    appears in the journal - all of which require manually watching a real run
    against Alpaca's paper API, not something this function proves by itself."""
    node = build_node()
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    run()
