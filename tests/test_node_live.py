"""wit/nautilus/node_live.py: the paper_only boot assertion and the config/
strategy assembly that don't need a live Alpaca/Polygon connection to test.
Actually connecting (`node.run()`) is the broker swap's Phase 7 staged
validation gate and is a manual, watched verification step against real paper
APIs - not something a unit test does.
"""
from __future__ import annotations

import pytest

from wit.committee.stub import StubPolicyProvider
from wit.config import AlpacaConfig, Config, SafetyConfig
from wit.nautilus import node_live
from wit.nautilus.actor import FundStateActor, FundStateActorConfig

PAPER = AlpacaConfig(api_key="PKTESTKEY", secret_key="test-secret", paper=True)


def _config_with_paper_only(value: bool) -> Config:
    # Config and SafetyConfig are both frozen - can't monkeypatch a single
    # nested field, so swap the whole module-level CONFIG reference instead.
    return Config(safety=SafetyConfig(paper_only=value))


# ── assert_paper_only ───────────────────────────────────────────────────

def test_paper_only_accepts_a_correctly_configured_paper_account(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    node_live.assert_paper_only(PAPER)  # must not raise


def test_paper_only_rejects_paper_flag_off(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    live = AlpacaConfig(api_key="PKTESTKEY", secret_key="test-secret", paper=False)
    with pytest.raises(node_live.PaperOnlyViolation, match="ALPACA_PAPER is not set"):
        node_live.assert_paper_only(live)


def test_paper_only_rejects_a_missing_api_key(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    missing = AlpacaConfig(api_key="", secret_key="test-secret", paper=True)
    with pytest.raises(node_live.PaperOnlyViolation, match="ALPACA_API_KEY/ALPACA_SECRET_KEY"):
        node_live.assert_paper_only(missing)


def test_paper_only_rejects_a_missing_secret_key(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    missing = AlpacaConfig(api_key="PKTESTKEY", secret_key="", paper=True)
    with pytest.raises(node_live.PaperOnlyViolation, match="ALPACA_API_KEY/ALPACA_SECRET_KEY"):
        node_live.assert_paper_only(missing)


def test_paper_only_rejects_when_the_safety_flag_itself_is_off(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(False))
    with pytest.raises(node_live.PaperOnlyViolation, match="WIT_PAPER_ONLY is not set"):
        node_live.assert_paper_only(PAPER)


def test_paper_only_checks_the_safety_flag_before_anything_else(monkeypatch):
    """Even a well-formed paper config must not pass if the hard lock itself
    is off - the flag is checked first, independent of what else is
    configured correctly."""
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(False))
    with pytest.raises(node_live.PaperOnlyViolation, match="WIT_PAPER_ONLY"):
        node_live.assert_paper_only(PAPER)


# ── build_config ─────────────────────────────────────────────────────────

def test_build_config_registers_exactly_one_polygon_data_and_alpaca_exec_client():
    config = node_live.build_config(PAPER)
    assert set(config.data_clients.keys()) == {"POLYGON"}
    assert set(config.exec_clients.keys()) == {"ALPACA"}


def test_build_config_starts_with_no_strategies_or_actors():
    """Confirms the F9 design constraint documented in node_live.py's module
    docstring: strategies/actors are added manually after node.build(), not
    through the config-driven factory path this class can't use."""
    config = node_live.build_config(PAPER)
    assert config.strategies == []
    assert config.actors == []


def test_build_config_uses_the_configured_alpaca_credentials():
    config = node_live.build_config(PAPER)
    exec_cfg = config.exec_clients["ALPACA"]
    data_cfg = config.data_clients["POLYGON"]
    assert exec_cfg.api_key == PAPER.api_key
    assert exec_cfg.secret_key == PAPER.secret_key
    assert exec_cfg.paper is True
    assert data_cfg.alpaca_api_key == PAPER.api_key
    assert data_cfg.alpaca_secret_key == PAPER.secret_key
    assert data_cfg.alpaca_paper is True


def test_build_config_instrument_provider_loads_exactly_the_watchlist_ids():
    config = node_live.build_config(PAPER)
    loaded = {str(i) for i in config.exec_clients["ALPACA"].instrument_provider.load_ids}
    expected = {pair[0] for pair in node_live.INSTRUMENT_IDS.values()}
    assert loaded == expected


def test_build_config_rejects_a_live_alpaca_config():
    """Phase N6 audit finding F9 (unchanged by the broker swap): build_config
    is public and asserts nothing itself unless it does this - relying
    entirely on callers (only build_node() in this repo today) to have
    checked first would be fragile."""
    live = AlpacaConfig(api_key="PKTESTKEY", secret_key="test-secret", paper=False)
    with pytest.raises(node_live.PaperOnlyViolation):
        node_live.build_config(live)


def test_build_config_routes_polygon_to_the_alpaca_venue():
    """The broker swap's load-bearing design decision (verified in Phase 0
    against installed nautilus_trader's data/engine.pyx register_venue_routing):
    Polygon's data client must be routed to serve ALPACA-addressed data
    commands even though its own client venue is None."""
    config = node_live.build_config(PAPER)
    routing = config.data_clients["POLYGON"].routing
    assert routing.venues == frozenset({"ALPACA"})


# ── build_strategies ─────────────────────────────────────────────────────

def _fund_state():
    return FundStateActor(FundStateActorConfig(
        venue=node_live.ALPACA_VENUE, kill_switch_file="unused_in_this_test",
        dream_state_path="unused_in_this_test",
    ))


def test_build_strategies_produces_one_per_mapped_watchlist_symbol():
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    symbols = {s.config.symbol for s in strategies}
    assert symbols == set(node_live.INSTRUMENT_IDS.keys())


def test_build_strategies_maps_instrument_ids_to_the_alpaca_venue():
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    by_symbol = {s.config.symbol: s for s in strategies}
    assert str(by_symbol["NVDA"].config.instrument_id) == "NVDA.ALPACA"
    assert str(by_symbol["AAPL"].config.instrument_id) == "AAPL.ALPACA"


def test_build_strategies_bar_type_instrument_matches_the_strategys_own_instrument():
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    for s in strategies:
        assert s.config.bar_type.instrument_id == s.config.instrument_id
        assert "1-HOUR" in str(s.config.bar_type)


def test_build_strategies_uses_last_price_for_every_equity():
    """Every watchlist symbol is a US equity now (EURUSD dropped - Alpaca has
    no forex), so every bar type uses LAST, not the IB build's per-asset-class
    MID/LAST split."""
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    for s in strategies:
        assert "-LAST-" in str(s.config.bar_type)


def test_build_strategies_leaves_account_venue_unset():
    """Unlike the IB build (which needed an explicit IB_VENUE override -
    Phase N6 audit finding F4), the broker swap's single-venue design means
    instrument_id.venue already IS ALPACA_VENUE, so WitStrategyConfig's own
    default (None -> resolved to instrument_id.venue) is correct without an
    override. See node_live.py's module docstring."""
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    for s in strategies:
        assert s.config.account_venue is None


def test_build_strategies_watchlist_has_no_forex():
    """EURUSD dropped - Alpaca has no forex (build plan's "Architecture"
    section)."""
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    assert "EURUSD" not in {s.config.symbol for s in strategies}


# ── _bar_step ────────────────────────────────────────────────────────────

def test_bar_step_maps_known_timeframes():
    assert node_live._bar_step("H1") == "1-HOUR"
    assert node_live._bar_step("M15") == "15-MINUTE"


def test_bar_step_rejects_an_unknown_timeframe():
    with pytest.raises(ValueError, match="no Nautilus bar-step mapping"):
        node_live._bar_step("W1")


# ── ALPACA_VENUE ─────────────────────────────────────────────────────────

def test_fund_state_is_configured_against_the_single_alpaca_venue():
    """The broker swap's design note: every InstrumentId - including the ones
    whose bars come from Polygon - uses ALPACA_VENUE (node_live.py's module
    docstring)."""
    assert str(node_live.ALPACA_VENUE) == "ALPACA"


# ── watched_bar_types / bar_interval_seconds (Phase N8 staleness watchdog) ─

def test_watched_bar_types_covers_the_whole_watchlist():
    bar_types = node_live.watched_bar_types()
    assert set(bar_types.keys()) == set(node_live.INSTRUMENT_IDS.keys())


def test_watched_bar_types_matches_build_strategies_own_bar_types():
    """Phase N6 audit finding F1 (IB build) was exactly two slightly-different
    copies of this string-building logic drifting apart - pin that the shared
    helper produces the identical strings build_strategies() actually
    subscribes to, not just similarly-shaped ones."""
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    from_strategies = {s.config.symbol: str(s.config.bar_type) for s in strategies}
    assert node_live.watched_bar_types() == from_strategies


def test_bar_interval_seconds_maps_known_timeframes():
    assert node_live.bar_interval_seconds("H1") == 3600
    assert node_live.bar_interval_seconds("M15") == 900


def test_bar_interval_seconds_rejects_an_unknown_timeframe():
    with pytest.raises(ValueError, match="no bar-interval mapping"):
        node_live.bar_interval_seconds("W1")
