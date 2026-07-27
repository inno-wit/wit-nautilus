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
    """Make numpy scalars and datetimes JSON-safe."""
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
        cycle_id: str = "",
    ) -> dict[str, Any]:
        return self.write({
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
        })

    def log_event(self, kind: str, message: str, **extra: Any) -> dict[str, Any]:
        return self.write({"type": "event", "kind": kind, "message": message, **extra})
