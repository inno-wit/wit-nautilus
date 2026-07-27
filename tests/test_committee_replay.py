"""ReplayCommitteeProvider: the SQLite decision cache backing backtest replay
(build plan §1.2 / §3 Phase N3). New code, not a port — the MT5 build has no
backtester.
"""
from __future__ import annotations

import pytest

from tests.conftest import make_bars
from wit.committee.contract import CommitteeDecision
from wit.committee.replay import CacheMissError, ReplayCommitteeProvider
from wit.committee.stub import StubPolicyProvider
from wit.desks import garch, markov, quant_analyst, technicals


def _report(symbol="EURUSD", drift=0.001):
    bars = make_bars(drift=drift)
    tech = technicals.compute(symbol, bars)
    mk = markov.compute(symbol, bars)
    gk = garch.compute(symbol, bars, "H1")
    return quant_analyst.compute(symbol, "H1", tech, mk, gk)


def test_strict_mode_raises_on_a_cache_miss(tmp_path):
    provider = ReplayCommitteeProvider(tmp_path / "cache.db", mode="strict")
    with pytest.raises(CacheMissError, match="no cached decision"):
        provider.decide(_report(), instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1000)


def test_record_mode_requires_a_live_provider():
    with pytest.raises(ValueError, match="live provider"):
        ReplayCommitteeProvider(":memory:", mode="record", live=None)


def test_record_mode_calls_through_and_caches(tmp_path):
    live = StubPolicyProvider(action="BUY", conviction=0.5)
    path = tmp_path / "cache.db"
    provider = ReplayCommitteeProvider(path, mode="record", live=live)

    report = _report()
    d1 = provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1000)
    assert d1.action == "BUY"
    assert live.calls == 1

    # Same key again: served from cache, live provider not called a second time.
    d2 = provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1000)
    assert d2 == d1
    assert live.calls == 1


def test_strict_mode_hits_what_record_mode_wrote(tmp_path):
    path = tmp_path / "cache.db"
    live = StubPolicyProvider(action="SELL", conviction=0.3)
    recorder = ReplayCommitteeProvider(path, mode="record", live=live)
    report = _report()
    recorder.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=42)
    recorder.close()

    replayer = ReplayCommitteeProvider(path, mode="strict")
    d = replayer.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=42)
    assert d.action == "SELL"
    assert d.conviction == 0.3


def test_cache_key_distinguishes_instrument_and_timestamp(tmp_path):
    live = StubPolicyProvider(action="BUY", conviction=0.5)
    provider = ReplayCommitteeProvider(tmp_path / "cache.db", mode="record", live=live)
    report = _report()

    provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1000)
    provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=2000)
    provider.decide(report, instrument_id="NVDA.SMART", bar_ts_ns=1000)
    assert live.calls == 3  # three distinct keys, no accidental collisions


def test_cache_key_distinguishes_report_content(tmp_path):
    """Same instrument/timestamp, different underlying report (e.g. a replay
    run against a differently-configured desk) must not collide."""
    live = StubPolicyProvider(action="BUY", conviction=0.5)
    provider = ReplayCommitteeProvider(tmp_path / "cache.db", mode="record", live=live)

    provider.decide(_report(drift=0.001), instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1000)
    provider.decide(_report(drift=-0.001), instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1000)
    assert live.calls == 2


def test_decision_round_trips_through_json_faithfully(tmp_path):
    live = StubPolicyProvider(action="BUY", conviction=0.5)
    provider = ReplayCommitteeProvider(tmp_path / "cache.db", mode="record", live=live)
    report = _report()
    written = provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)

    fresh = ReplayCommitteeProvider(tmp_path / "cache.db", mode="strict")
    reread = fresh.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)
    assert isinstance(reread, CommitteeDecision)
    assert reread == written
