"""``wit/ops/reflection.py`` — joins journaled decisions to realized P&L.

Phase N7's second cut (the audit-driven rewrite): P&L now comes straight
from ``WitStrategy.on_position_closed``'s own structured ``realized_pnl``
journal field, paired chronologically per symbol - not from Nautilus's
``position_id``, which the Phase N7 audit's finding C1 proved is not a
trade identifier under ``OmsType.NETTING`` (the only OMS this system
runs): it's a constant per ``(instrument, strategy)``, so a naive id-based
join either finds nothing or silently scores every decision on a symbol
against one surviving trade. These tests exercise the chronological
FIFO-per-symbol join against a real ``Journal`` (JSONL round-trip).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


def _log_round_trip(journal: Journal, symbol: str, pnl: float, conviction: float = 0.6,
                    coid: str = "O-1") -> None:
    """One executed decision immediately followed by its closing trade -
    the shape a single completed round trip takes in the journal."""
    journal.log_decision(
        symbol, _Decision(conviction=conviction), _Plan(), _Report(),
        order={"ok": True, "client_order_id": coid},
        client_order_id=coid,
    )
    journal.log_event("position_closed", f"realized_pnl={pnl}",
                      symbol=symbol, position_id=f"{symbol}.SIM-Strategy-000",
                      opening_order_id=coid, realized_pnl=pnl)


# ── Bucket ───────────────────────────────────────────────────────────────

def test_bucket_computes_win_rate_and_avg_pnl():
    b = Bucket()
    b.add(100.0)
    b.add(-40.0)
    d = b.as_dict()
    assert d == {"trades": 2, "win_rate": 0.5, "total_pnl": 60.0, "avg_pnl": 30.0}


def test_bucket_empty_reports_zeros_not_a_division_error():
    assert Bucket().as_dict() == {"trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}


# ── Reflection.review: the chronological per-symbol join ─────────────────

def test_review_scores_a_completed_round_trip(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _log_round_trip(journal, "NVDA", pnl=150.0)
    review = Reflection(journal).review(days=7)

    assert review["decisions_considered"] == 1
    assert review["trades_scored"] == 1
    assert review["overall"]["total_pnl"] == 150.0
    assert review["by_symbol"]["NVDA"]["trades"] == 1


def test_review_ignores_a_decision_whose_order_was_not_ok(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    journal.log_decision("NVDA", _Decision(), _Plan(action="HOLD"), _Report(),
                         order=None, client_order_id="")
    review = Reflection(journal).review(days=7)
    assert review["decisions_considered"] == 1
    assert review["trades_scored"] == 0


def test_review_scores_nothing_when_the_position_has_not_closed_yet(tmp_path):
    """An executed decision with no position_closed event yet (still open)
    must not be scored - and must not crash looking for one."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    journal.log_decision("NVDA", _Decision(), _Plan(), _Report(),
                         order={"ok": True, "client_order_id": "O-1"},
                         client_order_id="O-1")
    review = Reflection(journal).review(days=7)
    assert review["trades_scored"] == 0


def test_review_ignores_a_rejected_order_without_offsetting_later_trades(tmp_path):
    """Phase N7 audit finding F2 (round-8 verification): order.ok=True on a
    decision means the order was SUBMITTED, not that it FILLED - a
    rejected/cancelled order never gets an order_filled/position_closed
    event. The chronological-FIFO join this replaced would have consumed
    the NEXT real close on this symbol against the rejected decision,
    silently offsetting every later trade on that symbol by one slot. The
    exact opening_order_id match must not do that: a decision whose
    client_order_id never appears as an opening_order_id anywhere is simply
    excluded, with zero effect on any other decision's pairing."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    # Submitted, then rejected - no order_filled, no position_closed ever
    # references O-1.
    journal.log_decision("NVDA", _Decision(), _Plan(), _Report(),
                         order={"ok": True, "client_order_id": "O-1"},
                         client_order_id="O-1")
    # A genuine, later round trip on the same symbol.
    _log_round_trip(journal, "NVDA", pnl=75.0, coid="O-2")

    review = Reflection(journal).review(days=7)

    assert review["decisions_considered"] == 2
    assert review["trades_scored"] == 1
    assert review["overall"]["total_pnl"] == 75.0


def test_review_excludes_a_close_whose_entry_is_outside_the_window(tmp_path):
    """Phase N7 audit finding (round-8 verification): the chronological-FIFO
    join mis-attributed a close whose entry decision fell outside the
    review window to whatever in-window decision happened to be first in
    the FIFO queue. The exact-id join has no such coupling: a
    position_closed event whose opening_order_id matches no in-window
    decision is simply invisible, and every other decision's pairing is
    unaffected."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    # A close whose entry decision was never journalled in this window.
    journal.log_event("position_closed", "realized_pnl=-900.0", symbol="NVDA",
                      position_id="NVDA.SIM-Strategy-000",
                      opening_order_id="O-OUTSIDE-WINDOW", realized_pnl=-900.0)
    # A genuine in-window round trip that must score its own P&L only.
    _log_round_trip(journal, "NVDA", pnl=50.0, coid="O-2")

    review = Reflection(journal).review(days=7)

    assert review["trades_scored"] == 1
    assert review["overall"]["total_pnl"] == 50.0


def test_review_pairs_two_round_trips_on_the_same_symbol_by_exact_id(tmp_path):
    """The exact production shape the Phase N7 audit's finding C1 broke:
    two independent trades on one symbol, both carrying the SAME Nautilus
    position_id (a real NETTING artifact - see this module's docstring) but
    distinct opening_order_id/client_order_id values. Each decision must
    score against its OWN close, not one replicated across both - and,
    unlike the chronological-FIFO design this replaced, the order the
    events were journalled in must not matter."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _log_round_trip(journal, "NVDA", pnl=100.0, coid="O-1")
    _log_round_trip(journal, "NVDA", pnl=-40.0, coid="O-2")

    review = Reflection(journal).review(days=7)

    assert review["trades_scored"] == 2
    assert review["by_symbol"]["NVDA"] == {
        "trades": 2, "win_rate": 0.5, "total_pnl": 60.0, "avg_pnl": 30.0,
    }
    # Not the C1 failure mode: neither "0 trades scored" nor "2 trades
    # scored, both at the same P&L, 100% or 0% win rate".
    assert review["overall"]["win_rate"] != 1.0
    assert review["overall"]["win_rate"] != 0.0


def test_review_keeps_symbols_independent(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _log_round_trip(journal, "NVDA", pnl=100.0, coid="O-1")
    _log_round_trip(journal, "AAPL", pnl=-25.0, coid="O-2")

    review = Reflection(journal).review(days=7)

    assert review["trades_scored"] == 2
    assert review["by_symbol"]["NVDA"]["total_pnl"] == 100.0
    assert review["by_symbol"]["AAPL"]["total_pnl"] == -25.0


def test_review_buckets_by_conviction_and_regime(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _log_round_trip(journal, "NVDA", pnl=-50.0, conviction=0.8)
    review = Reflection(journal).review(days=7)
    assert review["by_conviction"]["0.6-1.0"]["trades"] == 1
    assert review["by_markov_regime"]["Bull"]["total_pnl"] == -50.0
    assert review["by_vol_regime"]["calm"]["trades"] == 1


def test_review_respects_the_window(tmp_path):
    """entries_since already filters by ts - a days=0 window must not pick
    up anything (regression guard on the plumbing, not the math)."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _log_round_trip(journal, "NVDA", pnl=150.0)
    review = Reflection(journal).review(days=0)
    assert review["decisions_considered"] == 0


def test_review_uses_the_passed_now_not_wall_clock(tmp_path):
    """Phase N7 audit finding H1: FundStateActor's daily timer runs on
    simulated time in a backtest, which can be months from wall-clock
    now() - review() must accept an explicit now rather than always
    deriving its window from the system clock."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _log_round_trip(journal, "NVDA", pnl=150.0)

    far_future = datetime.now(UTC) + timedelta(days=400)
    review = Reflection(journal).review(days=1, now=far_future)
    assert review["decisions_considered"] == 0, (
        "a 1-day window 400 days in the future must not see today's entries"
    )


# ── format ───────────────────────────────────────────────────────────────

def test_format_includes_headline_stats(tmp_path):
    journal = Journal(str(tmp_path / "journal.jsonl"))
    _log_round_trip(journal, "NVDA", pnl=150.0)
    review = Reflection(journal).review(days=7)
    text = Reflection.format(review)
    assert "1 decisions, 1 trades scored" in text
    assert "NVDA" in text
