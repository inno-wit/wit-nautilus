"""ReplayCommitteeProvider: the SQLite decision cache backing backtest replay
(build plan §1.2 / §3 Phase N3). New code, not a port — the MT5 build has no
backtester.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

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


# ── Thread safety (Phase N3 audit finding F2) ──────────────────────────────
# decide() is invoked via NautilusTrader's run_in_executor, which dispatches
# to a multi-worker thread pool - never the thread that constructed the
# provider. sqlite3's default check_same_thread=True makes that a hard
# ProgrammingError; these call decide() from a real worker thread to prove
# it doesn't.

def test_decide_works_from_a_worker_thread(tmp_path):
    live = StubPolicyProvider(action="BUY", conviction=0.5)
    provider = ReplayCommitteeProvider(tmp_path / "cache.db", mode="record", live=live)
    report = _report()

    with ThreadPoolExecutor(max_workers=1) as pool:
        d = pool.submit(
            provider.decide, report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1
        ).result()
    assert d.action == "BUY"


def test_concurrent_writes_from_multiple_worker_threads_do_not_corrupt_the_cache(tmp_path):
    live = StubPolicyProvider(action="BUY", conviction=0.5)
    provider = ReplayCommitteeProvider(tmp_path / "cache.db", mode="record", live=live)

    def decide_one(i: int):
        return provider.decide(
            _report(symbol=f"SYM{i}"), instrument_id=f"SYM{i}.SMART", bar_ts_ns=i
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(decide_one, range(20)))
    assert len(results) == 20
    assert all(d.action == "BUY" for d in results)
    assert live.calls == 20


# ── Don't cache abstains (Phase N3 audit finding F4) ───────────────────────

class _FlakyThenHealthyProvider:
    """Abstains once (simulating a transient outage), then answers normally."""

    def __init__(self):
        self.calls = 0

    def decide(self, report, *, instrument_id="", bar_ts_ns=0):
        self.calls += 1
        if self.calls == 1:
            return CommitteeDecision.abstain(report.symbol, "transient outage")
        return CommitteeDecision(
            symbol=report.symbol, action="BUY", conviction=0.5, risk_rating="medium",
            rationale="recovered", key_risk="none", stop_atr_mult=2.0, reward_risk=2.0,
        )


def test_a_transient_abstain_is_not_cached(tmp_path):
    live = _FlakyThenHealthyProvider()
    provider = ReplayCommitteeProvider(tmp_path / "cache.db", mode="record", live=live)
    report = _report()

    first = provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)
    assert first.action == "HOLD" and first.error is not None

    # Same key, live provider healthy this time: must NOT be served the cached
    # abstain, because there is no cached abstain - the key was never written.
    second = provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)
    assert second.action == "BUY"
    assert live.calls == 2


def test_strict_mode_still_misses_after_an_abstain_in_record_mode(tmp_path):
    path = tmp_path / "cache.db"
    live = _FlakyThenHealthyProvider()
    recorder = ReplayCommitteeProvider(path, mode="record", live=live)
    report = _report()
    recorder.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)  # abstains, not cached
    recorder.close()

    replayer = ReplayCommitteeProvider(path, mode="strict")
    with pytest.raises(CacheMissError):
        replayer.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)


# ── model_key in the cache key (Phase N3 audit finding F6) ─────────────────

def test_different_model_keys_do_not_share_a_cache_entry(tmp_path):
    path = tmp_path / "cache.db"
    live_a = StubPolicyProvider(action="BUY", conviction=0.5)
    report = _report()

    a = ReplayCommitteeProvider(path, mode="record", live=live_a, model_key="opus:sonnet")
    a.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)
    a.close()

    # Same instrument/timestamp/report, DIFFERENT model_key: must miss, not
    # silently replay the other model's decision.
    b = ReplayCommitteeProvider(path, mode="strict", model_key="haiku:haiku")
    with pytest.raises(CacheMissError):
        b.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)

    # The original model_key still hits.
    a_again = ReplayCommitteeProvider(path, mode="strict", model_key="opus:sonnet")
    assert a_again.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1).action == "BUY"


# ── JSON serialization hardening (Phase N3 audit finding F5) ───────────────

class _NumpyLikeScalar:
    """Duck-types a numpy scalar (has .item()) without adding a numpy import
    to this test file — the desks' array math is the realistic source."""

    def __init__(self, value):
        self._value = value

    def item(self):
        return self._value


def test_numpy_like_values_in_detail_are_coerced_on_write(tmp_path):
    class _ProviderWithNumpyDetail:
        def decide(self, report, *, instrument_id="", bar_ts_ns=0):
            return CommitteeDecision(
                symbol=report.symbol, action="BUY", conviction=0.5, risk_rating="medium",
                rationale="r", key_risk="k", stop_atr_mult=2.0, reward_risk=2.0,
                detail={"confidence": _NumpyLikeScalar(0.87), "n": _NumpyLikeScalar(3)},
            )

    provider = ReplayCommitteeProvider(
        tmp_path / "cache.db", mode="record", live=_ProviderWithNumpyDetail()
    )
    report = _report()
    # decide()'s immediate return value is the live provider's own object,
    # not round-tripped - the coercion only happens on the write to SQLite.
    provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)

    reread = ReplayCommitteeProvider(tmp_path / "cache.db", mode="strict").decide(
        report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1
    )
    assert reread.detail == {"confidence": 0.87, "n": 3}


def test_an_unknown_field_in_a_stale_cache_row_is_ignored_not_fatal(tmp_path):
    """A future CommitteeDecision schema change must not make every
    previously recorded cache unreadable - unknown keys are dropped on read
    rather than raising a TypeError from **raw."""
    import json

    path = tmp_path / "cache.db"
    live = StubPolicyProvider(action="BUY", conviction=0.5)
    provider = ReplayCommitteeProvider(path, mode="record", live=live)
    report = _report()
    provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)

    # Simulate a stale row from an older/newer schema by injecting an unknown key.
    row = provider._conn.execute(
        "SELECT decision_json FROM decisions WHERE bar_ts_ns = 1"
    ).fetchone()[0]
    stale = json.loads(row)
    stale["a_field_that_no_longer_exists"] = "legacy value"
    provider._conn.execute(
        "UPDATE decisions SET decision_json = ? WHERE bar_ts_ns = 1", (json.dumps(stale),)
    )
    provider._conn.commit()

    reread = provider.decide(report, instrument_id="EUR/USD.IDEALPRO", bar_ts_ns=1)
    assert reread.action == "BUY"  # did not raise TypeError on the unknown key


# ── DecisionProvider signature parity (Phase N3 audit finding F7) ──────────

def test_all_providers_match_the_protocol_signature_exactly():
    import inspect

    from wit.committee.live import LiveCommitteeProvider
    from wit.committee.provider import DecisionProvider

    expected = inspect.signature(DecisionProvider.decide)
    for cls in (LiveCommitteeProvider, StubPolicyProvider, ReplayCommitteeProvider):
        actual = inspect.signature(cls.decide)
        assert actual.parameters.keys() == expected.parameters.keys(), (
            f"{cls.__name__}.decide parameter names diverge from DecisionProvider: "
            f"{list(actual.parameters)} != {list(expected.parameters)}"
        )
        for name in ("instrument_id", "bar_ts_ns"):
            assert actual.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{cls.__name__}.decide's {name!r} must stay keyword-only"
            )
            assert actual.parameters[name].default == expected.parameters[name].default
