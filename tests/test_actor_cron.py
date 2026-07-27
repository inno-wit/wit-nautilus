"""``wit/nautilus/actor.py``'s ``_next_daily``/``_next_weekly`` - the pure
"next occurrence" math behind the daily briefing/review and weekly dream
timers (Phase N7). No Nautilus kernel needed for these; the timers
themselves (wired via ``Clock.set_timer``) are exercised end to end in
``tests/test_strategy_backtest.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime

from wit.nautilus.actor import _next_daily, _next_weekly


def test_next_daily_later_today():
    now = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)  # Monday
    assert _next_daily(now, 23, 55) == datetime(2026, 7, 27, 23, 55, tzinfo=UTC)


def test_next_daily_rolls_to_tomorrow_when_already_past():
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    assert _next_daily(now, 0, 5) == datetime(2026, 7, 28, 0, 5, tzinfo=UTC)


def test_next_daily_at_the_exact_moment_rolls_to_tomorrow():
    """A timer must not fire twice for the same instant - exactly-equal
    counts as already-happened, not still-pending."""
    now = datetime(2026, 7, 27, 0, 5, 0, tzinfo=UTC)
    assert _next_daily(now, 0, 5) == datetime(2026, 7, 28, 0, 5, tzinfo=UTC)


def test_next_weekly_finds_the_coming_sunday():
    monday = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
    assert monday.weekday() == 0
    nxt = _next_weekly(monday, 6, 22, 30)
    assert nxt.weekday() == 6
    assert nxt.date() == datetime(2026, 8, 2, tzinfo=UTC).date()
    assert (nxt.hour, nxt.minute) == (22, 30)


def test_next_weekly_rolls_a_full_week_when_already_past_this_sunday():
    sunday_23 = datetime(2026, 8, 2, 23, 0, tzinfo=UTC)  # after 22:30 target
    nxt = _next_weekly(sunday_23, 6, 22, 30)
    assert nxt.date() == datetime(2026, 8, 9, tzinfo=UTC).date()


def test_next_weekly_same_day_before_the_target_time():
    sunday_morning = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)
    nxt = _next_weekly(sunday_morning, 6, 22, 30)
    assert nxt == datetime(2026, 8, 2, 22, 30, tzinfo=UTC)
