"""``wit/ops/reflection.py`` — joins journaled decisions to realized P&L.

Unlike the MT5 build (one broker ticket per order, known at decision time),
a Nautilus decision only knows its ``client_order_id`` when journaled; the
``position_id`` it opened is only known once ``on_order_filled`` fires,
later, asynchronously. These tests exercise that two-step join against a
real ``Journal`` (JSONL round-trip), not a mock.
"""
from __future__ import annotations

from dataclasses import dataclass

from wit.ops.journal import Journal
from wit.ops.reflection import Bucket, Reflection


@dataclass
class _Decision:
    conviction: float = 0.6

    def to_dict(self):
        return {"conviction": self.conviction}


@dataclass
class _Plan:
    action: str = "BUY"

    def to_dict(self):
        return {"action": self.action}


@dataclass
class _Report:
    markov_regime: str = "Bull"
    vol_regime: str = "calm"

    def to_dict(self):
        return {
            "markov": {"regime": self.markov_regime},
            "garch": {"vol_regime": self.vol_regime},
        }


def _journal_with_one_closed_trade(tmp_path, symbol="NVDA", pnl=150.0, conviction=0.7):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    journal.log_decision(
        symbol, _Decision(conviction=conviction), _Plan(), _Report(),
        order={"ok": True, "client_order_id": "O-1"},
        client_order_id="O-1",
    )
    journal.log_event("order_filled", "BUY 1 @ 100", symbol=symbol,
                      client_order_id="O-1", position_id="P-1")
    journal.log_event("position_closed", f"realized_pnl={pnl}",
                      symbol=symbol, position_id="P-1")
    return journal


# ── Bucket ───────────────────────────────────────────────────────────────

def test_bucket_computes_win_rate_and_avg_pnl():
    b = Bucket()
    b.add(100.0)
    b.add(-40.0)
    d = b.as_dict()
    assert d == {"trades": 2, "win_rate": 0.5, "total_pnl": 60.0, "avg_pnl": 30.0}


def test_bucket_empty_reports_zeros_not_a_division_error():
    assert Bucket().as_dict() == {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}


# ── Reflection.review: the client_order_id -> position_id -> pnl join ─────

def test_review_scores_a_trade_via_the_fill_to_position_join(tmp_path):
    journal = _journal_with_one_closed_trade(tmp_path, symbol="NVDA", pnl=150.0)
    review = Reflection(journal).review({"P-1": 150.0}, days=7)

    assert review["decisions_considered"] == 1
    assert review["trades_scored"] == 1
    assert review["overall"]["total_pnl"] == 150.0
    assert review["by_symbol"]["NVDA"]["trades"] == 1


def test_review_ignores_a_decision_whose_order_was_not_ok(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    journal.log_decision("NVDA", _Decision(), _Plan(action="HOLD"), _Report(),
                         order=None, client_order_id="")
    review = Reflection(journal).review({}, days=7)
    assert review["decisions_considered"] == 1
    assert review["trades_scored"] == 0


def test_review_ignores_a_fill_with_no_matching_pnl_entry(tmp_path):
    """The position closed but the caller's pnl_by_position (e.g. outside
    the lookback window) doesn't have it - must not crash or double count."""
    journal = _journal_with_one_closed_trade(tmp_path, symbol="NVDA", pnl=150.0)
    review = Reflection(journal).review({}, days=7)
    assert review["trades_scored"] == 0


def test_review_does_not_confuse_two_client_order_ids(tmp_path):
    """A second decision's client_order_id must not accidentally match the
    first fill's position - only the exact recorded link counts."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    journal.log_decision("NVDA", _Decision(), _Plan(), _Report(),
                         order={"ok": True, "client_order_id": "O-1"},
                         client_order_id="O-1")
    journal.log_decision("AAPL", _Decision(), _Plan(), _Report(),
                         order={"ok": True, "client_order_id": "O-2"},
                         client_order_id="O-2")
    journal.log_event("order_filled", "fill", symbol="NVDA",
                      client_order_id="O-1", position_id="P-1")
    # AAPL's entry never fills in this window - O-2 has no linked position.
    review = Reflection(journal).review({"P-1": 75.0}, days=7)
    assert review["trades_scored"] == 1
    assert review["by_symbol"] == {"NVDA": {"trades": 1, "win_rate": 1.0,
                                            "total_pnl": 75.0, "avg_pnl": 75.0}}


def test_review_buckets_by_conviction_and_regime(tmp_path):
    journal = _journal_with_one_closed_trade(tmp_path, pnl=-50.0, conviction=0.8)
    review = Reflection(journal).review({"P-1": -50.0}, days=7)
    assert review["by_conviction"]["0.6-1.0"]["trades"] == 1
    assert review["by_markov_regime"]["Bull"]["total_pnl"] == -50.0
    assert review["by_vol_regime"]["calm"]["trades"] == 1


def test_review_respects_the_window(tmp_path):
    """entries_since already filters by ts - a days=0 window must not pick
    up anything (regression guard on the plumbing, not the math)."""
    journal = _journal_with_one_closed_trade(tmp_path)
    review = Reflection(journal).review({"P-1": 150.0}, days=0)
    assert review["decisions_considered"] == 0


# ── format ───────────────────────────────────────────────────────────────

def test_format_includes_headline_stats(tmp_path):
    journal = _journal_with_one_closed_trade(tmp_path, symbol="NVDA", pnl=150.0)
    review = Reflection(journal).review({"P-1": 150.0}, days=7)
    text = Reflection.format(review)
    assert "1 decisions, 1 trades scored" in text
    assert "NVDA" in text
