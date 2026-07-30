"""Phase N3 gate: the committee's decision contract holds under stress. Ported
from Wit-Hedge-fund/tests/test_committee.py (import paths + ``deliberate`` ->
``decide`` only) plus the rate-limiter tests from the same file.

The Anthropic client is stubbed — these assert the *engine's* guarantees (a
HOLD never carries size, an LLM failure abstains rather than trades), not the
model's judgement.
"""
from __future__ import annotations

import itertools
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tests.conftest import make_bars
from wit.committee.live import LiveCommitteeProvider, _RateLimiter
from wit.config import LLMConfig
from wit.desks import garch, markov, quant_analyst, technicals

PM_VERDICT = {
    "action": "BUY", "conviction": 0.55, "risk_rating": "medium",
    "rationale": "Markov regime and EMA structure agree.", "key_risk": "regime flip",
    "stop_atr_mult": 2.0, "reward_risk": 1.8,
}


class FakeMessages:
    """Returns a researcher text block, then a PM tool_use block.

    ``verdict`` also accepts the sentinels ``"NO_TOOL_CALL"`` (PM responds
    with text instead of calling the tool) and ``"EMPTY"`` (PM returns no
    content at all — e.g. a truncated or malformed gateway response).
    """

    def __init__(self, verdict, fail: bool = False):
        self.verdict, self.fail = verdict, fail
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("api unavailable")
        usage = SimpleNamespace(input_tokens=100, output_tokens=50)
        if "tools" not in kwargs:  # researcher turn
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="case text")],
                usage=usage, stop_reason="end_turn",
            )
        if self.verdict == "EMPTY":
            return SimpleNamespace(content=[], usage=usage, stop_reason="max_tokens")
        if self.verdict == "NO_TOOL_CALL":
            content = [SimpleNamespace(type="text", text="I decline to call the tool")]
            return SimpleNamespace(content=content, usage=usage, stop_reason="end_turn")
        content = [SimpleNamespace(type="tool_use", input=self.verdict)]
        return SimpleNamespace(content=content, usage=usage, stop_reason="tool_use")


def build_committee(verdict=PM_VERDICT, fail: bool = False) -> LiveCommitteeProvider:
    c = LiveCommitteeProvider.__new__(LiveCommitteeProvider)  # bypass __init__'s API-key check
    c.llm = SimpleNamespace(quick_model="q", deep_model="d", api_key="test",
                           nara_api_key="nara-test", rpm_limit=0)  # 0 = no throttling in tests
    c.timeframe = "H1"
    # Same fake backs both clients — FakeMessages already branches on whether
    # `tools` is in the call kwargs (researcher vs PM turn), not on which
    # client made the call.
    fake = FakeMessages(verdict, fail)
    c._pm_client = SimpleNamespace(messages=fake)
    c._quick_client = SimpleNamespace(messages=fake)
    c._limiter = _RateLimiter(c.llm.rpm_limit)
    return c


@pytest.fixture
def report():
    bars = make_bars(drift=0.001)
    tech = technicals.compute("EURUSD", bars)
    mk = markov.compute("EURUSD", bars)
    gk = garch.compute("EURUSD", bars, "H1")
    return quant_analyst.compute("EURUSD", "H1", tech, mk, gk)


def test_committee_returns_a_full_decision(report):
    d = build_committee().decide(report)
    assert d.action == "BUY"
    assert d.conviction == pytest.approx(0.55)
    assert d.bull_case and d.bear_case
    assert d.error is None


def test_hold_never_carries_conviction(report):
    verdict = {**PM_VERDICT, "action": "HOLD", "conviction": 0.9}
    d = build_committee(verdict).decide(report)
    assert d.action == "HOLD"
    assert d.conviction == 0.0


def test_conviction_is_clamped_to_unit_range(report):
    d = build_committee({**PM_VERDICT, "conviction": 3.7}).decide(report)
    assert d.conviction == 1.0


def test_llm_failure_abstains_instead_of_trading(report):
    d = build_committee(fail=True).decide(report)
    assert d.action == "HOLD"
    assert d.conviction == 0.0
    assert "api unavailable" in d.error


def test_missing_tool_call_abstains(report):
    d = build_committee(verdict="NO_TOOL_CALL").decide(report)
    assert d.action == "HOLD"
    assert "no tool call" in d.error


def test_empty_pm_content_abstains(report):
    """Regression: a malformed/truncated gateway response with no content
    blocks at all must not crash on `for b in msg.content` (None/[] isn't
    iterable-safe by assumption) — it should abstain with a clear reason."""
    d = build_committee(verdict="EMPTY").decide(report)
    assert d.action == "HOLD"
    assert "empty content" in d.error
    assert "max_tokens" in d.error


def test_empty_researcher_content_abstains(report):
    """Same regression, at the researcher call site instead of the PM's."""
    c = build_committee()
    c._quick_client.messages.create = lambda **kwargs: SimpleNamespace(
        content=[], usage=SimpleNamespace(input_tokens=10, output_tokens=0),
        stop_reason="max_tokens",
    )
    d = c.decide(report)
    assert d.action == "HOLD"
    assert "empty content" in d.error


def test_build_context_delegates_to_the_report(report):
    assert LiveCommitteeProvider.build_context(report) == report.as_prompt_block()


# ── Dream cycle (Phase N7) ─────────────────────────────────────────────────
# LiveCommitteeProvider.dream is pure LLM I/O — validating/attaching real
# basis numbers is wit.ops.dream's job. These only assert the call never
# raises and parses a valid response correctly.

class FakeDreamMessages:
    """Returns a submit_lessons tool_use block, or one of the same failure
    sentinels FakeMessages uses for the PM call."""

    def __init__(self, lessons, fail: bool = False):
        self.lessons, self.fail = lessons, fail

    def create(self, **kwargs):
        if self.fail:
            raise RuntimeError("api unavailable")
        usage = SimpleNamespace(input_tokens=50, output_tokens=30)
        if self.lessons == "EMPTY":
            return SimpleNamespace(content=[], usage=usage, stop_reason="max_tokens")
        if self.lessons == "NO_TOOL_CALL":
            content = [SimpleNamespace(type="text", text="I decline to call the tool")]
            return SimpleNamespace(content=content, usage=usage, stop_reason="end_turn")
        content = [SimpleNamespace(type="tool_use", input={"lessons": self.lessons})]
        return SimpleNamespace(content=content, usage=usage, stop_reason="tool_use")


def build_dream_committee(lessons, fail: bool = False) -> LiveCommitteeProvider:
    c = LiveCommitteeProvider.__new__(LiveCommitteeProvider)
    c.llm = SimpleNamespace(quick_model="q", deep_model="d", api_key="test",
                           nara_api_key="nara-test", rpm_limit=0)
    c.timeframe = "H1"
    # dream() only ever calls the PM client.
    c._pm_client = SimpleNamespace(messages=FakeDreamMessages(lessons, fail))
    c._limiter = _RateLimiter(c.llm.rpm_limit)
    return c


def test_dream_parses_a_valid_response():
    raw = [{"lesson": "NVDA BUYs underperform", "dimension": "symbol",
           "key": "NVDA", "confidence": "medium"}]
    result = build_dream_committee(raw).dream({"symbol": {"NVDA": {}}}, [], 30, 5)
    assert result == raw


def test_dream_never_raises_on_api_failure():
    assert build_dream_committee([], fail=True).dream({}, [], 30, 5) == []


def test_dream_never_raises_on_no_tool_call():
    assert build_dream_committee("NO_TOOL_CALL").dream({}, [], 30, 5) == []


def test_dream_never_raises_on_empty_content():
    assert build_dream_committee("EMPTY").dream({}, [], 30, 5) == []


# ── Rate limiter ─────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_paces_consecutive_calls(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("wit.committee.live.time.monotonic", clock.monotonic)
    monkeypatch.setattr("wit.committee.live.time.sleep", clock.sleep)

    limiter = _RateLimiter(rpm=10)  # 6s minimum spacing
    limiter.wait()                  # first call: nothing to wait for
    assert clock.slept == []

    clock.now += 2.0                # only 2s have "passed"
    limiter.wait()                  # must wait out the remaining 4s
    assert clock.slept == [4.0]


def test_rate_limiter_does_not_wait_once_the_interval_has_elapsed(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("wit.committee.live.time.monotonic", clock.monotonic)
    monkeypatch.setattr("wit.committee.live.time.sleep", clock.sleep)

    limiter = _RateLimiter(rpm=10)
    limiter.wait()
    clock.now += 10.0                # well past the 6s minimum interval
    limiter.wait()
    assert clock.slept == []


def test_rate_limiter_disabled_at_zero_never_sleeps(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr("wit.committee.live.time.monotonic", clock.monotonic)
    monkeypatch.setattr("wit.committee.live.time.sleep", clock.sleep)

    limiter = _RateLimiter(rpm=0)
    limiter.wait()
    limiter.wait()
    assert clock.slept == []


def test_committee_researcher_and_pm_calls_share_one_limiter(monkeypatch):
    """The PM shares the same account-wide budget as the researchers — a
    cheap way to catch a regression where a second limiter gets constructed
    per call instead of once per provider."""
    clock = FakeClock()
    monkeypatch.setattr("wit.committee.live.time.monotonic", clock.monotonic)
    monkeypatch.setattr("wit.committee.live.time.sleep", clock.sleep)

    committee = build_committee()
    committee.llm = SimpleNamespace(quick_model="q", deep_model="d", api_key="test",
                                    rpm_limit=10)
    committee._limiter = _RateLimiter(committee.llm.rpm_limit)

    bars = make_bars(drift=0.001, seed=11)
    tech = technicals.compute("EURUSD", bars)
    mk = markov.compute("EURUSD", bars)
    gk = garch.compute("EURUSD", bars, "H1")
    report = quant_analyst.compute("EURUSD", "H1", tech, mk, gk)
    committee.decide(report)

    # 3 calls (bull, bear, PM) at 10rpm => 2 waits of ~6s each; the first call
    # is always free.
    assert len(clock.slept) == 2
    assert all(s == pytest.approx(6.0) for s in clock.slept)


def test_rate_limiter_serializes_concurrent_callers():
    """Phase N3 audit finding F3: wait() must hold its lock across the sleep,
    not just the read-modify-write, or concurrent callers (real, unfaked
    threads here) compute their delay from the same stale _last_call and wake
    in a burst instead of being paced apart."""
    limiter = _RateLimiter(rpm=600)  # 0.1s spacing - fast enough for a real test
    calls: list[float] = []
    lock = threading.Lock()

    def worker():
        limiter.wait()
        with lock:
            calls.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    calls.sort()
    gaps = [b - a for a, b in itertools.pairwise(calls)]
    assert all(gap >= 0.08 for gap in gaps), (
        f"calls were not serialized apart: gaps={gaps}"
    )


# ── __init__ construction (Phase N3 audit finding F1) ─────────────────────
# build_committee() above bypasses __init__ via __new__ for every other test
# in this file, which is how a broken rpm_limit reference on a real LLMConfig
# survived to a commit: nothing ever exercised construction. These do.

def test_init_constructs_pm_and_quick_clients_and_wires_the_limiter(monkeypatch):
    calls: list[dict] = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))

    llm = LLMConfig(
        api_key="test-key", deep_model="d", quick_model="q",
        nara_api_key="nara-key", nara_base_url="https://router.bynara.id",
        rpm_limit=30,
    )
    provider = LiveCommitteeProvider(llm=llm, timeframe="H1")

    assert len(calls) == 2  # PM client, then quick client
    pm_kwargs, quick_kwargs = calls
    assert pm_kwargs["api_key"] == "test-key"
    assert "base_url" not in pm_kwargs  # empty base_url must not be forwarded
    assert quick_kwargs["api_key"] == "nara-key"
    assert quick_kwargs["base_url"] == "https://router.bynara.id"
    assert isinstance(provider._limiter, _RateLimiter)
    assert provider._limiter._min_interval == pytest.approx(2.0)  # 60/30


def test_init_does_not_forward_an_explicitly_empty_nara_base_url(monkeypatch):
    """opus-audit round 1 finding: an explicit-but-empty NARA_BASE_URL in .env
    (overriding LLMConfig's non-empty default) must not reach the SDK as
    ``base_url=""`` — that's a malformed request URL, not "use the default",
    and would silently abstain every symbol to HOLD."""
    calls: list[dict] = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))

    llm = LLMConfig(api_key="test-key", deep_model="d", quick_model="q",
                    nara_api_key="nara-key", nara_base_url="", rpm_limit=30)
    LiveCommitteeProvider(llm=llm, timeframe="H1")

    _, quick_kwargs = calls
    assert "base_url" not in quick_kwargs


def test_init_rejects_a_missing_api_key():
    llm = LLMConfig(api_key="", deep_model="d", quick_model="q", nara_api_key="nara-key")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        LiveCommitteeProvider(llm=llm)


def test_init_rejects_a_missing_nara_api_key():
    llm = LLMConfig(api_key="test-key", deep_model="d", quick_model="q", nara_api_key="")
    with pytest.raises(ValueError, match="NARA_API_KEY"):
        LiveCommitteeProvider(llm=llm)


def test_init_rejects_missing_models():
    llm = LLMConfig(api_key="test-key", deep_model="", quick_model="", nara_api_key="nara-key")
    with pytest.raises(ValueError, match="WIT_DEEP_MODEL"):
        LiveCommitteeProvider(llm=llm)


# ── Malformed tool-call payload (Phase N3 audit finding F10) ──────────────

def test_pm_tool_call_missing_a_required_key_abstains(report):
    """The gate names this failure mode explicitly: a tool_use block whose
    input is missing a required field (e.g. a truncated/malformed response)
    must abstain, not raise KeyError out of decide()."""
    incomplete = {k: v for k, v in PM_VERDICT.items() if k != "risk_rating"}
    d = build_committee(incomplete).decide(report)
    assert d.action == "HOLD"
    assert d.conviction == 0.0
    assert "risk_rating" in d.error


def test_pm_tool_call_non_numeric_conviction_abstains(report):
    bad = {**PM_VERDICT, "conviction": "not-a-number"}
    d = build_committee(bad).decide(report)
    assert d.action == "HOLD"
    assert d.error is not None
