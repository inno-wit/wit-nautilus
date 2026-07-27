"""``wit/ops/dream.py``'s orchestration half (Phase N7): ``run()``,
``format_digest()``, and the guardrail helpers (``_qualifying_buckets``,
``_score_previous_lessons``, ``_build_lessons``). The state layer
(``DreamState``/``load``/``save``) is covered separately in
``tests/test_dream_state.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from wit.ops import dream
from wit.ops.journal import Journal

# ── _qualifying_buckets / _score_previous_lessons / _build_lessons ────────

def test_qualifying_buckets_drops_thin_buckets():
    review = {
        "by_symbol": {"NVDA": {"trades": 10, "win_rate": 0.6, "avg_pnl": 5.0},
                      "AAPL": {"trades": 2, "win_rate": 0.5, "avg_pnl": 1.0}},
        "by_markov_regime": {}, "by_vol_regime": {}, "by_conviction": {},
    }
    out = dream._qualifying_buckets(review, min_trades=5)
    assert out == {"symbol": {"NVDA": review["by_symbol"]["NVDA"]}}


def test_qualifying_buckets_omits_a_dimension_with_nothing_qualifying():
    review = {"by_symbol": {"AAPL": {"trades": 1}}, "by_markov_regime": {},
              "by_vol_regime": {}, "by_conviction": {}}
    assert dream._qualifying_buckets(review, min_trades=5) == {}


def test_score_previous_lessons_reports_no_trades_since_as_none():
    previous = dream.DreamState(
        dream_id="d0", generated_at="x", window_days=30, decisions_considered=0,
        trades_scored=0,
        lessons=[dream.Lesson("l1", "lesson text", "symbol", "NVDA", "low", 8, 0.25, -5.0)],
    )
    review = {"by_symbol": {}}
    scores = dream._score_previous_lessons(previous, review)
    assert scores[0].trades_since == 0
    assert scores[0].win_rate_since is None
    assert scores[0].pnl_since is None


def test_score_previous_lessons_reports_real_numbers_when_the_bucket_recurs():
    previous = dream.DreamState(
        dream_id="d0", generated_at="x", window_days=30, decisions_considered=0,
        trades_scored=0,
        lessons=[dream.Lesson("l1", "lesson text", "symbol", "NVDA", "low", 8, 0.25, -5.0)],
    )
    review = {"by_symbol": {"NVDA": {"trades": 4, "win_rate": 0.75, "total_pnl": 40.0}}}
    scores = dream._score_previous_lessons(previous, review)
    assert scores[0].trades_since == 4
    assert scores[0].win_rate_since == 0.75
    assert scores[0].pnl_since == 40.0


def test_build_lessons_drops_a_hallucinated_bucket():
    qualifying = {"symbol": {"NVDA": {"trades": 8, "win_rate": 0.25, "avg_pnl": -5.0}}}
    raw = [
        {"lesson": "real", "dimension": "symbol", "key": "NVDA", "confidence": "high"},
        {"lesson": "hallucinated", "dimension": "symbol", "key": "TSLA", "confidence": "high"},
    ]
    lessons = dream._build_lessons(raw, qualifying)
    assert len(lessons) == 1
    assert lessons[0].lesson == "real"
    assert lessons[0].basis_trades == 8


def test_build_lessons_fills_basis_numbers_from_qualifying_not_the_llm():
    qualifying = {"symbol": {"NVDA": {"trades": 8, "win_rate": 0.25, "avg_pnl": -5.0}}}
    raw = [{"lesson": "x", "dimension": "symbol", "key": "NVDA", "confidence": "high",
           "trades": 999, "win_rate": 0.99}]  # LLM-reported numbers must be ignored
    lessons = dream._build_lessons(raw, qualifying)
    assert lessons[0].basis_trades == 8
    assert lessons[0].basis_win_rate == 0.25


# ── run() / format_digest() ─────────────────────────────────────────────

class _FakeDreamCommittee:
    def __init__(self, lessons):
        self._lessons = lessons
        self.calls = 0

    def dream(self, qualifying, scores, window_days, min_bucket_trades):
        self.calls += 1
        return self._lessons


@dataclass
class _Decision:
    conviction: float = 0.7

    def to_dict(self):
        return {"conviction": self.conviction}


@dataclass
class _Plan:
    action: str = "BUY"

    def to_dict(self):
        return {"action": self.action}


@dataclass
class _Report:
    def to_dict(self):
        return {"markov": {"regime": "Bull"}, "garch": {"vol_regime": "calm"}}


def _journal_with_n_closed_trades(tmp_path, n: int, pnl: float = 20.0):
    """``n`` completed round trips on NVDA, all sharing the same Nautilus
    position_id - the real NETTING shape (Phase N7 audit finding C1) that
    made the id-based join this replaced score 0 or N copies of one trade."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    for i in range(n):
        coid = f"O-{i}"
        journal.log_decision("NVDA", _Decision(), _Plan(), _Report(),
                             order={"ok": True, "client_order_id": coid},
                             client_order_id=coid)
        journal.log_event("position_closed", f"realized_pnl={pnl}",
                          symbol="NVDA", position_id="NVDA.SIM-Strategy-000",
                          opening_order_id=coid, realized_pnl=pnl)
    return journal


def test_run_produces_no_lessons_below_the_sample_floor(tmp_path):
    journal = _journal_with_n_closed_trades(tmp_path, n=2)
    committee = _FakeDreamCommittee([])
    from wit.config import DreamConfig
    cfg = DreamConfig(state_path=str(tmp_path / "state.json"), window_days=30,
                      min_bucket_trades=5)

    state = dream.run(committee, journal, cfg)

    assert state.lessons == []
    assert committee.calls == 0  # never even asked - qualifying was empty
    assert state.trades_scored == 2


def test_run_calls_the_committee_and_saves_state_above_the_sample_floor(tmp_path):
    journal = _journal_with_n_closed_trades(tmp_path, n=5)
    raw = [{"lesson": "NVDA BUYs win here", "dimension": "symbol",
           "key": "NVDA", "confidence": "medium"}]
    committee = _FakeDreamCommittee(raw)
    from wit.config import DreamConfig
    cfg = DreamConfig(state_path=str(tmp_path / "state.json"), window_days=30,
                      min_bucket_trades=5)

    state = dream.run(committee, journal, cfg)

    assert committee.calls == 1
    assert len(state.lessons) == 1
    assert state.lessons[0].basis_trades == 5
    assert dream.load(cfg.state_path).lessons[0].lesson == "NVDA BUYs win here"


def test_run_uses_the_passed_now_not_wall_clock(tmp_path):
    """Phase N5 audit F1/F3's bug class, re-caught by the Phase N7 audit's
    finding H1: FundStateActor's weekly timer runs on simulated time in a
    backtest, which can be months away from wall-clock "now" - dream.run()
    must use the caller's clock (here, an explicit `now`), never a bare
    datetime.now(UTC) default, for generated_at and for the review window."""
    journal = _journal_with_n_closed_trades(tmp_path, n=2)
    committee = _FakeDreamCommittee([])
    from wit.config import DreamConfig
    cfg = DreamConfig(state_path=str(tmp_path / "state.json"))
    simulated_now = datetime(2026, 1, 4, 22, 30, tzinfo=UTC)

    state = dream.run(committee, journal, cfg, now=simulated_now)

    assert state.generated_at == simulated_now.isoformat()


def test_run_journals_a_dream_cycle_event(tmp_path):
    journal = _journal_with_n_closed_trades(tmp_path, n=2)
    committee = _FakeDreamCommittee([])
    from wit.config import DreamConfig
    cfg = DreamConfig(state_path=str(tmp_path / "state.json"))

    dream.run(committee, journal, cfg)

    events = [r for r in journal.read() if r.get("kind") == "dream_cycle"]
    assert len(events) == 1


def test_run_scores_the_previous_cycles_lessons(tmp_path):
    journal = _journal_with_n_closed_trades(tmp_path, n=5, pnl=20.0)
    from wit.config import DreamConfig
    cfg = DreamConfig(state_path=str(tmp_path / "state.json"), min_bucket_trades=5)
    dream.save(dream.DreamState(
        dream_id="prev", generated_at="x", window_days=30, decisions_considered=0,
        trades_scored=0,
        lessons=[dream.Lesson("l1", "old lesson", "symbol", "NVDA", "low", 8, 0.25, -5.0)],
    ), cfg.state_path)
    committee = _FakeDreamCommittee([])

    state = dream.run(committee, journal, cfg)

    assert len(state.scores) == 1
    assert state.scores[0].trades_since == 5
    assert state.scores[0].pnl_since == 100.0  # 5 trades * 20.0


# ── format_digest ────────────────────────────────────────────────────────

def test_format_digest_reports_no_lessons_case():
    state = dream.DreamState(dream_id="d1", generated_at="2026-07-01T00:00:00",
                             window_days=30, decisions_considered=5, trades_scored=2)
    text = dream.format_digest(state)
    assert "No new lessons this cycle" in text


def test_format_digest_lists_new_lessons_and_prior_scores():
    state = dream.DreamState(
        dream_id="d1", generated_at="2026-07-01T00:00:00", window_days=30,
        decisions_considered=10, trades_scored=5,
        lessons=[dream.Lesson("l2", "new lesson", "symbol", "NVDA", "medium", 5, 0.6, 20.0)],
        scores=[dream.LessonScore("l1", "old lesson", "symbol", "AAPL", 8, 0.25, 4, 0.5, 12.0)],
    )
    text = dream.format_digest(state)
    assert "new lesson" in text
    assert "old lesson" in text
    assert "4 trades, 50% win rate" in text
