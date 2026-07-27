"""Per-instrument market-hours awareness.

Individual US equities quote only during the regular US cash session
(09:30-16:00 America/New_York, Mon-Fri) — FX/metals/index CFDs trade 24/5.
Convening the committee on a closed equity spends three LLM calls to produce
a HOLD the market forces anyway, and an approved plan there would only be
rejected by the broker.

Deliberately conservative in scope:
- Only *equities* (``SessionConfig.equity_symbols``) are gated.
- DST is handled by resolving the wall-clock in ``America/New_York`` via
  ``zoneinfo`` rather than hard-coding a UTC offset.
- **Fail-open:** if the tz database is unavailable, or the check is disabled,
  the symbol is treated as tradeable.

Ported from ``Wit-Hedge-fund/engine/market_hours.py`` (Phase N2), decoupled
from ``CommitteeDecision`` per the build plan §1 mapping table: ``is_tradeable``
returns a plain ``(bool, str)`` here; the HOLD decision itself is constructed
at the call site (``wit/nautilus/strategy.py``, Phase N5) where the committee
contract is already in scope.
"""
from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from wit.config import SessionConfig

_NY = "America/New_York"
_FX_WEEK_CLOSE = time(17, 0)  # Friday 17:00 ET - the standard forex week close
_FX_WEEK_OPEN = time(17, 0)   # Sunday 17:00 ET - the standard forex week open


def _equity_cash_session_open(weekday: int, t: time, cfg: SessionConfig) -> bool:
    """Is a US equity's regular cash session open right now (local NY wall-clock
    already resolved by the caller)? Pure market-structure fact - shared by
    ``is_tradeable`` (gated behind ``enforce_equity_hours``) and
    ``is_session_open`` (deliberately NOT gated behind that flag - see its
    docstring)."""
    return weekday < 5 and cfg.cash_open <= t < cfg.cash_close


def is_tradeable(
    symbol: str, cfg: SessionConfig, now: datetime | None = None
) -> tuple[bool, str]:
    """Return ``(tradeable?, reason)`` for ``symbol`` at ``now`` (UTC).

    ``reason`` is empty when tradeable and a human-readable closure reason
    otherwise. Non-equities and the disabled/tz-unavailable cases all return
    ``(True, "")``.
    """
    if not cfg.enforce_equity_hours or symbol.upper() not in cfg.equity_symbols:
        return True, ""
    now = now or datetime.now(UTC)
    try:
        local = now.astimezone(ZoneInfo(_NY))
    except Exception:  # noqa: BLE001 - missing tzdata must not block trading
        return True, ""
    weekday, t = local.weekday(), local.timetz().replace(tzinfo=None)
    if _equity_cash_session_open(weekday, t, cfg):
        return True, ""
    if weekday >= 5:
        return False, "US equity market closed (weekend)"
    return (
        False,
        (f"outside US equity cash session "
         f"({cfg.cash_open:%H:%M}–{cfg.cash_close:%H:%M} ET)"),
    )


def is_session_open(symbol: str, cfg: SessionConfig, now: datetime | None = None) -> bool:
    """Whether ``symbol``'s market is open right now - unlike ``is_tradeable``,
    covers *every* watchlist symbol, not only equities, because the Phase N8
    bar staleness watchdog needs to know when *any* instrument is expected to
    be silent (a closed equity session, or FX's Friday-evening-to-Sunday-
    evening weekend close), not just when the committee should skip an
    equity that happens to be closed. ``is_tradeable`` stays untouched and
    equity-only - this function delegates to it for equities and adds the
    FX weekend rule for everything else, rather than widening
    ``is_tradeable``'s own scope (that function's callers assume "equities
    only" and must not silently start gating FX too).

    Fail-open, same as ``is_tradeable``: missing tzdata returns ``True``.
    """
    now = now or datetime.now(UTC)
    try:
        local = now.astimezone(ZoneInfo(_NY))
    except Exception:  # noqa: BLE001 - missing tzdata must not block trading
        return True
    weekday, t = local.weekday(), local.timetz().replace(tzinfo=None)
    if symbol.upper() in cfg.equity_symbols:
        # Deliberately NOT gated behind `cfg.enforce_equity_hours` (Phase N8
        # round-10 audit, Medium finding): that flag exists to control whether
        # the COMMITTEE skips a closed equity, a policy choice - it says nothing
        # about whether the exchange is physically producing bars right now,
        # which is a market-structure fact. Gating this branch on the same flag
        # meant WIT_EQUITY_HOURS=false silently reinstated finding C1 (nightly
        # false-halt) for every equity on the watchlist, purely as a side effect
        # of a flag whose only documented purpose is the committee-skip above.
        return _equity_cash_session_open(weekday, t, cfg)
    if weekday == 4 and t >= _FX_WEEK_CLOSE:   # Friday, after the week close
        return False
    if weekday == 5:                            # all of Saturday
        return False
    return not (weekday == 6 and t < _FX_WEEK_OPEN)   # Sunday, before the week open
