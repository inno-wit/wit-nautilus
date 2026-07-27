"""Per-instrument market-hours awareness (wit/ops/market_hours.py), ported from
Wit-Hedge-fund/tests/test_phase9.py's market_hours section.

Unlike the MT5 build, ``is_tradeable`` here returns a plain ``(bool, str)`` —
the HOLD-construction half moves to the strategy call site in Phase N5, since
that's where the committee contract is in scope (see the build plan §1
mapping table and wit/ops/market_hours.py's module docstring).
"""
from __future__ import annotations

from datetime import UTC, datetime

from wit.config import SessionConfig
from wit.ops import market_hours

CFG = SessionConfig()
TUE_OPEN = datetime(2026, 7, 21, 17, 0, tzinfo=UTC)   # Tue 13:00 ET
MON_CLOSED = datetime(2026, 7, 21, 2, 0, tzinfo=UTC)  # Mon 22:00 ET (after close)
SAT = datetime(2026, 7, 25, 17, 0, tzinfo=UTC)        # Sat 13:00 ET


def test_equity_open_during_us_cash_session():
    ok, reason = market_hours.is_tradeable("NVDA", CFG, TUE_OPEN)
    assert ok and reason == ""


def test_equity_closed_outside_cash_session():
    ok, reason = market_hours.is_tradeable("NVDA", CFG, MON_CLOSED)
    assert not ok and "cash session" in reason


def test_equity_closed_on_weekend():
    ok, reason = market_hours.is_tradeable("AAPL", CFG, SAT)
    assert not ok and "weekend" in reason


def test_fx_and_metals_are_always_tradeable():
    for sym in ("EURUSD", "XAUUSD", "US500"):   # US500 index CFD is not gated
        assert market_hours.is_tradeable(sym, CFG, MON_CLOSED) == (True, "")


def test_enforcement_can_be_disabled():
    off = SessionConfig(enforce_equity_hours=False)
    assert market_hours.is_tradeable("NVDA", off, MON_CLOSED) == (True, "")
