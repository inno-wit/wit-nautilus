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

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from wit.config import SessionConfig

_NY = "America/New_York"


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
    if local.weekday() >= 5:
        return False, "US equity market closed (weekend)"
    t = local.timetz().replace(tzinfo=None)
    if cfg.cash_open <= t < cfg.cash_close:
        return True, ""
    return (
        False,
        (f"outside US equity cash session "
         f"({cfg.cash_open:%H:%M}–{cfg.cash_close:%H:%M} ET)"),
    )
