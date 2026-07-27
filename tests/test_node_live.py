"""wit/nautilus/node_live.py: the paper_only boot assertion and the config/
strategy assembly that don't need a live IB connection to test. Actually
connecting (`node.run()`) is the build plan's Phase N6 gate and is a manual,
watched verification step against a real TWS/Gateway session - not something
a unit test does.
"""
from __future__ import annotations

import pytest

from wit.committee.stub import StubPolicyProvider
from wit.config import Config, IBConfig, SafetyConfig
from wit.nautilus import node_live
from wit.nautilus.actor import FundStateActor, FundStateActorConfig

PAPER = IBConfig(host="127.0.0.1", port=7497, client_id=1, account_id="DUR305728")


def _config_with_paper_only(value: bool) -> Config:
    # Config and SafetyConfig are both frozen - can't monkeypatch a single
    # nested field, so swap the whole module-level CONFIG reference instead.
    return Config(safety=SafetyConfig(paper_only=value))


# ── assert_paper_only ───────────────────────────────────────────────────

def test_paper_only_accepts_a_correctly_configured_paper_account(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    node_live.assert_paper_only(PAPER)  # must not raise


def test_paper_only_accepts_the_gateway_paper_port(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    gateway = IBConfig(host="127.0.0.1", port=4002, client_id=1, account_id="DUR305728")
    node_live.assert_paper_only(gateway)  # must not raise


def test_paper_only_rejects_the_live_tws_port(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    live_port = IBConfig(host="127.0.0.1", port=7496, client_id=1, account_id="DUR305728")
    with pytest.raises(node_live.PaperOnlyViolation, match="not a recognized paper port"):
        node_live.assert_paper_only(live_port)


def test_paper_only_rejects_the_live_gateway_port(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    live_port = IBConfig(host="127.0.0.1", port=4001, client_id=1, account_id="DUR305728")
    with pytest.raises(node_live.PaperOnlyViolation, match="not a recognized paper port"):
        node_live.assert_paper_only(live_port)


def test_paper_only_rejects_a_live_account_prefix(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    live_account = IBConfig(host="127.0.0.1", port=7497, client_id=1, account_id="U1234567")
    with pytest.raises(node_live.PaperOnlyViolation, match="does not start with 'DU'"):
        node_live.assert_paper_only(live_account)


def test_paper_only_rejects_an_empty_account_id(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(True))
    empty = IBConfig(host="127.0.0.1", port=7497, client_id=1, account_id="")
    with pytest.raises(node_live.PaperOnlyViolation, match="does not start with 'DU'"):
        node_live.assert_paper_only(empty)


def test_paper_only_rejects_when_the_safety_flag_itself_is_off(monkeypatch):
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(False))
    with pytest.raises(node_live.PaperOnlyViolation, match="WIT_PAPER_ONLY is not set"):
        node_live.assert_paper_only(PAPER)


def test_paper_only_checks_the_safety_flag_before_anything_else(monkeypatch):
    """Even a well-formed paper account/port must not pass if the hard lock
    itself is off - the flag is checked first, independent of what else is
    configured correctly."""
    monkeypatch.setattr(node_live, "CONFIG", _config_with_paper_only(False))
    with pytest.raises(node_live.PaperOnlyViolation, match="WIT_PAPER_ONLY"):
        node_live.assert_paper_only(PAPER)


# ── build_config ─────────────────────────────────────────────────────────

def test_build_config_registers_exactly_one_ib_data_and_exec_client():
    config = node_live.build_config(PAPER)
    assert set(config.data_clients.keys()) == {"IB"}
    assert set(config.exec_clients.keys()) == {"IB"}


def test_build_config_starts_with_no_strategies_or_actors():
    """Confirms the F9 design constraint documented in node_live.py's module
    docstring: strategies/actors are added manually after node.build(), not
    through the config-driven factory path this class can't use."""
    config = node_live.build_config(PAPER)
    assert config.strategies == []
    assert config.actors == []


def test_build_config_uses_the_configured_host_port_and_client_id():
    config = node_live.build_config(PAPER)
    data_cfg = config.data_clients["IB"]
    exec_cfg = config.exec_clients["IB"]
    assert data_cfg.ibg_host == PAPER.host == exec_cfg.ibg_host
    assert data_cfg.ibg_port == PAPER.port == exec_cfg.ibg_port
    assert data_cfg.ibg_client_id == PAPER.client_id == exec_cfg.ibg_client_id
    assert exec_cfg.account_id == PAPER.account_id


def test_build_config_instrument_provider_loads_exactly_the_watchlist_ids():
    config = node_live.build_config(PAPER)
    loaded = set(config.data_clients["IB"].instrument_provider.load_ids)
    assert loaded == set(node_live.INSTRUMENT_IDS.values())


# ── build_strategies ─────────────────────────────────────────────────────

def _fund_state():
    return FundStateActor(FundStateActorConfig(
        venue=node_live.IB_VENUE, kill_switch_file="unused_in_this_test",
    ))


def test_build_strategies_produces_one_per_mapped_watchlist_symbol():
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    symbols = {s.config.symbol for s in strategies}
    assert symbols == set(node_live.INSTRUMENT_IDS.keys())


def test_build_strategies_maps_instrument_ids_correctly():
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    by_symbol = {s.config.symbol: s for s in strategies}
    assert str(by_symbol["EURUSD"].config.instrument_id) == "EUR/USD.IDEALPRO"
    assert str(by_symbol["NVDA"].config.instrument_id) == "NVDA.SMART"


def test_build_strategies_bar_type_matches_the_configured_timeframe():
    strategies = node_live.build_strategies(StubPolicyProvider(), _fund_state())
    for s in strategies:
        assert "1-HOUR" in str(s.config.bar_type)


# ── _bar_step ────────────────────────────────────────────────────────────

def test_bar_step_maps_known_timeframes():
    assert node_live._bar_step("H1") == "1-HOUR"
    assert node_live._bar_step("M15") == "15-MINUTE"


def test_bar_step_rejects_an_unknown_timeframe():
    with pytest.raises(ValueError, match="no Nautilus bar-step mapping"):
        node_live._bar_step("W1")


# ── IB_VENUE ─────────────────────────────────────────────────────────────

def test_fund_state_is_configured_against_the_ib_account_venue_not_an_exchange():
    """Phase N6 design note: the account lives under IB_VENUE
    ("INTERACTIVE_BROKERS"), not under an instrument-routing venue like
    SMART or IDEALPRO - a fund trading both FX and equities still has one
    account/one equity figure."""
    assert str(node_live.IB_VENUE) == "INTERACTIVE_BROKERS"
