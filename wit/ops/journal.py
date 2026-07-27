"""Decision journal — an append-only JSONL record of everything the fund does.

Every bar writes one record per symbol, whether or not a trade resulted. The
blocked ones matter most: they are the audit trail showing the risk gates
fired. JSONL keeps it append-only and crash-safe, and it is what the weekly
performance review (Phase N7) reads.

Pulled forward from Phase N7 into N5, because ``WitStrategy`` needs it to log
decisions per the build plan's own Phase N5 `_on_decision` sequence
("build_plan -> safety re-check -> revalidate_plan -> bracket order ->
journal"). Ported from ``Wit-Hedge-fund/engine/journal.py`` with two additions
named in the build plan: ``position_id``/``client_order_id`` fields on
``log_decision``, since Nautilus positions are keyed differently than MT5
tickets. ``last_executed_ts`` (MT5's entry-based cooldown lookup) is NOT
ported — Nautilus's `on_position_closed` gives the strategy real exit
timestamps via `self.cache.positions_closed()`, making the cooldown
exit-aware for free (see `wit/nautilus/strategy.py`'s module docstring)
instead of the entry-based approximation MT5 needed.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wit.config import CONFIG


def _default(o: Any) -> Any:
    """Make numpy scalars/arrays and datetimes JSON-safe.

    Checks ``tolist`` before ``item`` (Phase N5 audit finding F12): a
    multi-element numpy array has both attributes, but ``.item()`` raises
    ``ValueError`` on anything but a size-1 array — and since that's not the
    ``TypeError`` ``json.dumps`` expects from a serializer hook, it used to
    escape ``write()`` uncaught. ``.tolist()`` handles arrays of any shape.
    """
    if hasattr(o, "tolist"):
        return o.tolist()
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


@dataclass
class Journal:
    path: Path = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.path = Path(self.path or CONFIG.journal_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        # setdefault, not unconditional: log_decision/log_event's ts=
        # parameter (Phase N7 audit finding, round 8) lets a Nautilus
        # caller stamp a record with its OWN clock - self.clock.utc_now()
        # in live, simulated time in a backtest - instead of the real
        # wall-clock default below. Without this, entries_since()'s window
        # filtering was comparing a simulated cutoff against wall-clock-
        # stamped entries, silently turning "last N simulated days" into
        # "everything written in the last few seconds of real time".
        record.setdefault("ts", datetime.now(UTC).isoformat())
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=_default) + "\n")
        return record

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)

    def entries_since(self, since: datetime) -> list[dict[str, Any]]:
        out = []
        for rec in self.read():
            try:
                if datetime.fromisoformat(rec["ts"]) >= since:
                    out.append(rec)
            except (KeyError, ValueError):
                continue
        return out

    # -- record builders -------------------------------------------------
    def log_decision(
        self, symbol: str, decision, plan, report,
        order: dict | None = None, position_id: str = "", client_order_id: str = "",
        cycle_id: str = "", ts: datetime | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "type": "decision",
            "cycle_id": cycle_id,
            "symbol": symbol,
            "action": plan.action,
            "executed": bool(order and order.get("ok")),
            "position_id": position_id,
            "client_order_id": client_order_id,
            "committee": decision.to_dict(),
            "plan": plan.to_dict(),
            "quant": report.to_dict(),
            "order": order,
        }
        if ts is not None:
            record["ts"] = ts.isoformat()
        return self.write(record)

    def log_event(self, kind: str, message: str, ts: datetime | None = None,
                  **extra: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"type": "event", "kind": kind, "message": message, **extra}
        if ts is not None:
            record["ts"] = ts.isoformat()
        return self.write(record)
