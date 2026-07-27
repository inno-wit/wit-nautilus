"""StubPolicyProvider: a fixed-verdict DecisionProvider for tests/CI/sweeps."""
from __future__ import annotations

from tests.conftest import make_bars
from wit.committee.stub import StubPolicyProvider
from wit.desks import garch, markov, quant_analyst, technicals


def _report():
    bars = make_bars(drift=0.001)
    tech = technicals.compute("EURUSD", bars)
    mk = markov.compute("EURUSD", bars)
    gk = garch.compute("EURUSD", bars, "H1")
    return quant_analyst.compute("EURUSD", "H1", tech, mk, gk)


def test_stub_returns_the_configured_action_and_conviction():
    provider = StubPolicyProvider(action="BUY", conviction=0.6)
    d = provider.decide(_report())
    assert d.action == "BUY"
    assert d.conviction == 0.6
    assert d.symbol == "EURUSD"


def test_stub_hold_never_carries_conviction():
    provider = StubPolicyProvider(action="HOLD", conviction=0.9)
    d = provider.decide(_report())
    assert d.action == "HOLD"
    assert d.conviction == 0.0


def test_stub_counts_calls():
    provider = StubPolicyProvider()
    provider.decide(_report())
    provider.decide(_report())
    assert provider.calls == 2


def test_stub_ignores_replay_only_kwargs():
    provider = StubPolicyProvider(action="SELL", conviction=0.4)
    d = provider.decide(_report(), instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=123)
    assert d.action == "SELL"
