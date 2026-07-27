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
from nautilus_trader.model.identifiers import InstrumentId, TraderId

from wit.committee.live import LiveCommitteeProvider
from wit.committee.provider import DecisionProvider
from wit.config import CONFIG, IBConfig
from wit.nautilus.actor import FundStateActor, FundStateActorConfig
from wit.nautilus.strategy import WitStrategy, WitStrategyConfig
from wit.ops.journal import Journal

# 7497 = TWS paper, 4002 = IB Gateway paper (build plan §3 Phase N6 / Phase N0
# confirmed live). 7496/4001 are the corresponding LIVE ports and are never
# accepted here, regardless of WIT_PAPER_ONLY.
PAPER_PORTS = (7497, 4002)

# Watchlist symbol (the logical, MT5-style form desks/committee/risk key
# everything by) -> Nautilus/IB InstrumentId string. Confirmed live against
# TWS in Phase N0 (exchange=SMART for the equities, IDEALPRO for EURUSD).
# Only the confirmed 8 have an entry - see wit/config.py's watchlist comment
# and the Phase N4 audit (finding F3) for why metals/index aren't here.
INSTRUMENT_IDS: dict[str, str] = {
    "EURUSD": "EUR/USD.IDEALPRO",
    "NVDA": "NVDA.SMART",
    "MSFT": "MSFT.SMART",
    "AAPL": "AAPL.SMART",
    "AMZN": "AMZN.SMART",
    "GOOGL": "GOOGL.SMART",
    "META": "META.SMART",
    "TSLA": "TSLA.SMART",
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
            f"(7497 TWS / 4002 Gateway) - refusing to boot."
        )
    if not ib.account_id.startswith("DU"):
        raise PaperOnlyViolation(
            f"TWS_ACCOUNT={ib.account_id!r} does not start with 'DU' (the IBKR "
            f"paper-account prefix) - refusing to boot. Live accounts start with 'U'."
        )


def build_config(ib: IBConfig | None = None) -> TradingNodeConfig:
    """The `TradingNode` config. Empty `strategies`/`actors` lists on purpose
    - see this module's docstring for why they're added manually after
    `node.build()` instead."""
    ib = ib or CONFIG.ib
    provider_cfg = InteractiveBrokersInstrumentProviderConfig(
        load_ids=frozenset(INSTRUMENT_IDS.values()),
    )
    data_cfg = InteractiveBrokersDataClientConfig(
        ibg_host=ib.host, ibg_port=ib.port, ibg_client_id=ib.client_id,
        instrument_provider=provider_cfg,
        use_regular_trading_hours=True,
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


def build_strategies(
    provider: DecisionProvider, fund_state: FundStateActor, journal: Journal | None = None,
) -> list[WitStrategy]:
    """One `WitStrategy` per watchlist symbol with a known IB instrument id."""
    strategies = []
    for symbol in CONFIG.watchlist:
        ib_id = INSTRUMENT_IDS.get(symbol)
        if ib_id is None:
            continue
        instrument_id = InstrumentId.from_str(ib_id)
        bar_type = BarType.from_str(f"{ib_id}-1-{_bar_step(CONFIG.timeframe)}-LAST-EXTERNAL")
        config = WitStrategyConfig(
            instrument_id=instrument_id, bar_type=bar_type, symbol=symbol,
            timeframe=CONFIG.timeframe, history_bars=CONFIG.history_bars,
        )
        strategies.append(WitStrategy(config, provider=provider, fund_state=fund_state,
                                      journal=journal))
    return strategies


def _bar_step(timeframe: str) -> str:
    """MT5-style timeframe ("H1", "M15") -> Nautilus BarType step string
    ("1-HOUR", "15-MINUTE"). Only the step used by the current watchlist
    (CONFIG.timeframe = "H1") is implemented; extend when a per-symbol
    timeframe is actually needed."""
    mapping = {"H1": "1-HOUR", "M15": "15-MINUTE", "M30": "30-MINUTE", "H4": "4-HOUR", "D1": "1-DAY"}
    if timeframe not in mapping:
        raise ValueError(f"no Nautilus bar-step mapping for timeframe {timeframe!r}")
    return mapping[timeframe]


def build_node() -> TradingNode:
    """Boot-asserts, builds, and fully wires a `TradingNode` (data/exec client
    factories registered, `node.build()` called) but does NOT call
    `node.run()` - kept separate so tests can exercise everything up to a
    live connection without one."""
    assert_paper_only()

    config = build_config()
    node = TradingNode(config=config)
    node.add_data_client_factory("IB", InteractiveBrokersLiveDataClientFactory)
    node.add_exec_client_factory("IB", InteractiveBrokersLiveExecClientFactory)
    node.build()

    fund_state = FundStateActor(FundStateActorConfig(
        venue=IB_VENUE,
        kill_switch_file=CONFIG.safety.kill_switch_file,
        dream_state_path=CONFIG.dream_state_path,
    ))
    provider = LiveCommitteeProvider()
    journal = Journal(CONFIG.journal_path)

    node.trader.add_actor(fund_state)
    for strategy in build_strategies(provider, fund_state, journal):
        node.trader.add_strategy(strategy)

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
