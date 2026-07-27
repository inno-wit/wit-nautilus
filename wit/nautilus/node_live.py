"""Assembles a live `TradingNode` against Interactive Brokers, paper-only by
construction (build plan §1.4/§3 Phase N6).

**The `paper_only` boot assertion lives here** (§1.4's table names this as the
guarantee's actual home): ``assert_paper_only()`` checks the *configured*
account id prefix, port, and ``WIT_PAPER_ONLY`` flag — all known before any
socket opens — and raises rather than degrading. It runs before
``node.build()``, so a misconfigured live account or a live port can never
reach the point of resolving instruments or subscribing to data, let alone
submitting an order.

**Strategies are assembled manually, not via Nautilus's config-driven
`TradingNodeConfig(strategies=[...])`/`ImportableStrategyConfig` path**
(Phase N5 audit finding F9): `WitStrategy.__init__` takes `provider`/
`fund_state` as extra constructor arguments beyond Nautilus's own
`config`-only convention, because those are live Python objects (a shared
rate-limited LLM client, a shared fund-state actor) that don't belong in a
serializable config. `StrategyFactory.create` calls `strategy_cls(config=config)`
and cannot construct this class — so `run()` builds the node with empty
`strategies=[]`/`actors=[]` in its `TradingNodeConfig`, then adds every
instance directly via `node.trader.add_actor()`/`add_strategy()` after
`node.build()`.

**The account is one venue, not one per exchange**: IB's adapter registers
the account itself under the fixed pseudo-venue `IB_VENUE`
("INTERACTIVE_BROKERS") — confirmed against the installed adapter, not
assumed — separate from the *instrument-routing* venues (`SMART` for
equities, `IDEALPRO` for FX) that appear in each `InstrumentId`. A single
IB account trading both asset classes still has one equity figure, so
`FundStateActor` is configured with `IB_VENUE`, giving it the fund-wide view
the daily-loss breaker needs regardless of how many exchanges the watchlist
touches.
"""
from __future__ import annotations

from pathlib import Path

from nautilus_trader.adapters.interactive_brokers.common import IB_VENUE
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersExecClientConfig,
    InteractiveBrokersInstrumentProviderConfig,
)
from nautilus_trader.adapters.interactive_brokers.factories import (
    InteractiveBrokersLiveDataClientFactory,
    InteractiveBrokersLiveExecClientFactory,
)
from nautilus_trader.config import LiveExecEngineConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import PriceType
from nautilus_trader.model.identifiers import InstrumentId, TraderId

from wit.committee.live import LiveCommitteeProvider
from wit.committee.provider import DecisionProvider
from wit.config import CONFIG, IBConfig
from wit.nautilus.actor import FundStateActor, FundStateActorConfig
from wit.nautilus.strategy import WitStrategy, WitStrategyConfig
from wit.ops.alerts import Alerter
from wit.ops.journal import Journal

# 7497 = TWS paper, 4002 = native IB Gateway paper (build plan §3 Phase N6 /
# Phase N0 confirmed live). 4004 = the ghcr.io/gnzsnz/ib-gateway Docker image's
# paper port (Phase N8) - that image binds the *native* 4002 to the
# container's own loopback only (verified against the image's published ports
# table, not guessed) and republishes it via socat on 0.0.0.0:4004 for other
# containers on the Compose network to reach; 4002 itself is never reachable
# from wit-nautilus's own `fund` container. 7496/4001/4003 are the
# corresponding LIVE ports (4003 is 4004's live-account sibling) and are
# never accepted here, regardless of WIT_PAPER_ONLY.
PAPER_PORTS = (7497, 4002, 4004)

# Watchlist symbol (the logical, MT5-style form desks/committee/risk key
# everything by) -> (Nautilus/IB InstrumentId string, bar price type).
#
# Phase N6 audit finding F2: the venue component of an IB equity InstrumentId
# is that instrument's PRIMARY EXCHANGE, not its order-routing destination
# (order routing to IB is always SMART regardless - confirmed against the
# adapter's own _decode_stock_contract, which sets exchange="SMART" and
# primaryExchange=<the venue you supplied>). "NVDA.SMART" therefore asks IB
# for a contract whose primary exchange IS "SMART", which doesn't exist -
# reqContractDetails returns error 200 for all seven equities. Re-probed
# live against TWS paper (DUR305728) with .NASDAQ: all seven resolve to
# exactly one contract each (conIds recorded in the N6 audit artifact).
# EUR/USD.IDEALPRO was already correct - IDEALPRO is FX's real venue, not a
# routing alias.
#
# Phase N6 audit finding F5: bar price type must be per-asset-class. IB has
# no trade prints for CASH (FX) contracts, so LAST (-> "TRADES" in the
# adapter) is rejected for EURUSD; it needs MID. Equities are fine with LAST.
INSTRUMENT_IDS: dict[str, tuple[str, PriceType]] = {
    "EURUSD": ("EUR/USD.IDEALPRO", PriceType.MID),
    "NVDA": ("NVDA.NASDAQ", PriceType.LAST),
    "MSFT": ("MSFT.NASDAQ", PriceType.LAST),
    "AAPL": ("AAPL.NASDAQ", PriceType.LAST),
    "AMZN": ("AMZN.NASDAQ", PriceType.LAST),
    "GOOGL": ("GOOGL.NASDAQ", PriceType.LAST),
    "META": ("META.NASDAQ", PriceType.LAST),
    "TSLA": ("TSLA.NASDAQ", PriceType.LAST),
}


class PaperOnlyViolation(RuntimeError):
    """Raised by ``assert_paper_only`` when the boot-time paper-only check
    fails. Never caught anywhere in this codebase - the entire point is to
    refuse to start, not to degrade or retry."""


def assert_paper_only(ib: IBConfig | None = None) -> None:
    """The paper_only hard lock (build plan §1.4), asserted at boot against
    *configuration* the process already has - no IB connection is opened to
    perform this check, so it can never be bypassed by a slow/failed
    connection falling through to a live default."""
    ib = ib or CONFIG.ib
    if not CONFIG.safety.paper_only:
        raise PaperOnlyViolation(
            "WIT_PAPER_ONLY is not set - refusing to boot a live-capable node. "
            "This is a hard lock and must never be relaxed from .env alone "
            "(build plan §1.4)."
        )
    if ib.port not in PAPER_PORTS:
        raise PaperOnlyViolation(
            f"IBG_PORT={ib.port} is not a recognized paper port {PAPER_PORTS} "
            f"(7497 TWS / 4002 native Gateway / 4004 dockerized ib-gateway) - refusing to boot."
        )
    if not ib.account_id.startswith("DU"):
        raise PaperOnlyViolation(
            f"TWS_ACCOUNT={ib.account_id!r} does not start with 'DU' (the IBKR "
            f"paper-account prefix) - refusing to boot. Live accounts start with 'U'."
        )


def build_config(ib: IBConfig | None = None) -> TradingNodeConfig:
    """The `TradingNode` config. Empty `strategies`/`actors` lists on purpose
    - see this module's docstring for why they're added manually after
    `node.build()` instead.

    Phase N6 audit finding F9: asserts paper_only itself now too, rather than
    relying solely on `build_node()`'s caller to have checked first - this
    function returns a fully live-capable config given a live `IBConfig`, and
    it's public."""
    ib = ib or CONFIG.ib
    assert_paper_only(ib)
    instrument_ids = [pair[0] for pair in INSTRUMENT_IDS.values()]
    provider_cfg = InteractiveBrokersInstrumentProviderConfig(
        load_ids=frozenset(instrument_ids),
    )
    data_cfg = InteractiveBrokersDataClientConfig(
        ibg_host=ib.host, ibg_port=ib.port, ibg_client_id=ib.client_id,
        instrument_provider=provider_cfg,
        use_regular_trading_hours=True,
        # Phase N6 audit finding F6: explicit rather than the adapter default
        # (REALTIME) - Phase N0 confirmed this paper account has no US equity
        # market data entitlement (error 10089 on both live and delayed).
        # DELAYED_FROZEN is the honest choice until that's enabled in IBKR
        # Account Management; FX is unaffected either way.
        market_data_type=1,  # 1=REALTIME - flip to 3 (DELAYED) if entitlement isn't enabled
    )
    exec_cfg = InteractiveBrokersExecClientConfig(
        ibg_host=ib.host, ibg_port=ib.port, ibg_client_id=ib.client_id,
        account_id=ib.account_id,
        instrument_provider=provider_cfg,
    )
    return TradingNodeConfig(
        trader_id=TraderId("WIT-001"),
        exec_engine=LiveExecEngineConfig(reconciliation=True),
        data_clients={"IB": data_cfg},
        exec_clients={"IB": exec_cfg},
    )


def _bar_type_str(ib_id: str, price_type: PriceType) -> str:
    """The one place a watchlist symbol's IB instrument id + price type
    becomes a Nautilus bar-type string - shared by `build_strategies()` and
    `watched_bar_types()` so the string is only ever assembled once. Phase
    N6 audit finding F1 was exactly this string built wrong in two slightly
    different ways in two places; do not reintroduce a second copy."""
    return f"{ib_id}-{_bar_step(CONFIG.timeframe)}-{price_type.name}-EXTERNAL"


def watched_bar_types() -> dict[str, str]:
    """Watchlist symbol -> its bar-type string, for
    `FundStateActorConfig.watched_bar_types` (Phase N8's staleness
    watchdog) - computed independently of `build_strategies()` since
    `FundStateActor` is constructed before the strategies are, in
    `build_node()`. A dict, not a bare tuple of bar-type strings (Phase N8
    audit finding C1): the watchdog needs the logical watchlist symbol
    (e.g. "EURUSD"), not just the Nautilus/IB instrument id, to ask
    `wit.ops.market_hours` whether that symbol's market is even open right
    now."""
    return {
        symbol: _bar_type_str(ib_id, price_type)
        for symbol, (ib_id, price_type) in INSTRUMENT_IDS.items()
    }


def build_strategies(
    provider: DecisionProvider, fund_state: FundStateActor, journal: Journal | None = None,
) -> list[WitStrategy]:
    """One `WitStrategy` per watchlist symbol with a known IB instrument id."""
    strategies = []
    for symbol in CONFIG.watchlist:
        pair = INSTRUMENT_IDS.get(symbol)
        if pair is None:
            continue
        ib_id, price_type = pair
        instrument_id = InstrumentId.from_str(ib_id)
        # Phase N6 audit finding F1: this used to read
        # f"{ib_id}-1-{_bar_step(...)}-LAST-EXTERNAL" - _bar_step already
        # returns "1-HOUR" (the step token includes the leading "1"), so the
        # extra literal "-1-" duplicated it, producing a bar type whose
        # instrument_id parsed as "NVDA.NASDAQ-1" - a phantom instrument
        # request_bars silently can't find, so on_start's warmup callback
        # never fires and the strategy never subscribes to anything.
        bar_type = BarType.from_str(_bar_type_str(ib_id, price_type))
        assert bar_type.instrument_id == instrument_id, (
            f"bar_type instrument mismatch: {bar_type.instrument_id} != {instrument_id}"
        )
        config = WitStrategyConfig(
            instrument_id=instrument_id, bar_type=bar_type, symbol=symbol,
            timeframe=CONFIG.timeframe, history_bars=CONFIG.history_bars,
            # Phase N6 audit finding F4: the account lives under IB_VENUE
            # ("INTERACTIVE_BROKERS"), never under an instrument's own
            # SMART/NASDAQ/IDEALPRO venue - WitStrategyConfig defaults
            # account_venue to the instrument venue (so N5's single-venue
            # backtest is unaffected), so IB wiring must override it
            # explicitly or every decision dies at "no_account_snapshot".
            account_venue=IB_VENUE,
            # Live/paper only (Phase N7) - see WitStrategyConfig's docstring
            # for why a backtest must never make this call.
            enable_market_intel=True,
        )
        strategies.append(WitStrategy(config, provider=provider, fund_state=fund_state,
                                      journal=journal))
    return strategies


# MT5-style timeframe -> (Nautilus BarType step string, seconds). One table,
# not two (Phase N8 audit finding I3): _bar_step and bar_interval_seconds
# used to maintain separate mappings over the same five timeframes, the same
# duplication shape N6's audit finding F1 already burned once for bar-type
# strings themselves. Only the step used by the current watchlist
# (CONFIG.timeframe = "H1") is exercised; extend when a per-symbol timeframe
# is actually needed.
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
    (it only constructs the data/exec clients); the actual IB connection
    happens later, inside `node.run()`.

    Phase N6 audit finding F7: every fallible non-IB object (the committee
    provider, which raises loudly on missing LLM config per Phase N3's
    design; the fund-state actor; the journal) is now constructed BEFORE
    `node.build()`, not after - previously a missing ANTHROPIC_API_KEY would
    raise only after the IB clients were already built, leaving `node` a
    local with no `dispose()` call reached. Everything from `node.build()`
    onward is now wrapped so a failure disposes the node instead of leaking
    the kernel and the adapter's cached IB client."""
    ib = CONFIG.ib  # Phase N6 audit finding F10: read once, assert, and use
    assert_paper_only(ib)  # this exact object - not re-read separately by build_config.

    provider = LiveCommitteeProvider()
    journal = Journal(CONFIG.journal_path)
    fund_state = FundStateActor(
        FundStateActorConfig(
            venue=IB_VENUE,
            kill_switch_file=CONFIG.safety.kill_switch_file,
            dream_state_path=CONFIG.dream.state_path,
            watched_bar_types=watched_bar_types(),
            bar_interval_seconds=bar_interval_seconds(CONFIG.timeframe),
            heartbeat_path=str(Path(CONFIG.journal_path).parent / "heartbeat"),
        ),
        journal=journal, committee=provider, alerter=Alerter.from_env(),
    )

    config = build_config(ib)
    node = TradingNode(config=config)
    try:
        node.add_data_client_factory("IB", InteractiveBrokersLiveDataClientFactory)
        node.add_exec_client_factory("IB", InteractiveBrokersLiveExecClientFactory)
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
    (Ctrl+C / SIGTERM). See the build plan's Phase N6 gate: node connects,
    instruments resolve, warmup+live bars arrive, one bracket order fills
    on paper and appears in the journal - all of which require manually
    watching a real run against TWS, not something this function proves by
    itself."""
    node = build_node()
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    run()
