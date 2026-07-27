"""Reflection — score closed trades against the thesis that opened them.

The journal records why each position was taken. Reflection joins that to the
realized outcome so the fund can see *which kinds of reasoning* made money,
not just which trades did. This is the input to the weekly dream cycle and
the daily/weekly Telegram review.

Deliberately statistics-first: it reports edge by symbol, Markov regime, vol
regime and conviction bucket. No LLM is required to produce it.

Ported from ``Wit-Hedge-fund/engine/reflection.py`` (Phase N7). The join key
changes: the MT5 build had one broker-side ticket per order, so
``review()`` took ``deals_by_ticket: dict[int, float]`` straight from the
broker and matched it against ``rec["order"]["ticket"]``. Nautilus positions
are not identified at decision time - a decision's journal record carries
only ``client_order_id`` (the entry order), and the ``position_id`` a fill
creates is only known once ``on_order_filled`` fires, asynchronously, after
the committee has already returned its verdict (see
``wit/nautilus/strategy.py``'s ``_on_decision``/``on_order_filled``). So the
join here is two-step instead of one: resolve each decision's
``client_order_id`` to a ``position_id`` via the ``order_filled`` events in
the same journal, then look that position up in the caller-supplied
``pnl_by_position`` (built from ``cache.positions_closed()`` - see
``wit/ops/dream.py``'s ``run()``). The aggregation logic itself (win rate by
symbol/regime/vol-regime/conviction) is unchanged.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from wit.ops.journal import Journal


@dataclass
class Bucket:
    n: int = 0
    wins: int = 0
    pnl: float = 0.0

    def add(self, pnl: float) -> None:
        self.n += 1
        self.pnl += pnl
        if pnl > 0:
            self.wins += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.n,
            "win_rate": round(self.wins / self.n, 3) if self.n else 0.0,
            "total_pnl": round(self.pnl, 2),
            "avg_pnl": round(self.pnl / self.n, 2) if self.n else 0.0,
        }


def _position_by_client_order_id(entries: list[dict[str, Any]]) -> dict[str, str]:
    """The entry fill links a decision's ``client_order_id`` to the
    ``position_id`` it opened. Only the *first* fill recorded for a given
    ``client_order_id`` is kept - that's the entry; a bracket's SL/TP legs
    fill under their own, different ``client_order_id`` and never appear
    here as a key."""
    out: dict[str, str] = {}
    for rec in entries:
        if rec.get("type") != "event" or rec.get("kind") != "order_filled":
            continue
        coid, pid = rec.get("client_order_id"), rec.get("position_id")
        if coid and pid and coid not in out:
            out[coid] = pid
    return out


@dataclass
class Reflection:
    journal: Journal = field(default_factory=Journal)

    @staticmethod
    def _conviction_bucket(c: float) -> str:
        return "0.0-0.3" if c < 0.3 else "0.3-0.6" if c < 0.6 else "0.6-1.0"

    def review(self, pnl_by_position: dict[str, float], days: int = 7) -> dict[str, Any]:
        """Join journaled decisions to realized P&L keyed by Nautilus
        position id.

        ``pnl_by_position`` maps a closed position's id to its realized P&L;
        the caller supplies it from ``cache.positions_closed()`` (see
        ``wit/ops/dream.py``'s ``run()``), so this module stays independent
        of Nautilus's own types.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        entries = self.journal.entries_since(since)
        position_by_coid = _position_by_client_order_id(entries)

        overall = Bucket()
        by_symbol: dict[str, Bucket] = defaultdict(Bucket)
        by_regime: dict[str, Bucket] = defaultdict(Bucket)
        by_conviction: dict[str, Bucket] = defaultdict(Bucket)
        by_vol_regime: dict[str, Bucket] = defaultdict(Bucket)

        considered = executed = 0
        for rec in entries:
            if rec.get("type") != "decision":
                continue
            considered += 1
            order = rec.get("order") or {}
            coid = rec.get("client_order_id") or order.get("client_order_id")
            if not (order.get("ok") and coid):
                continue
            position_id = position_by_coid.get(coid)
            if position_id is None or position_id not in pnl_by_position:
                continue

            executed += 1
            pnl = pnl_by_position[position_id]
            quant = rec.get("quant", {})
            committee = rec.get("committee", {})

            overall.add(pnl)
            by_symbol[rec["symbol"]].add(pnl)
            by_regime[quant.get("markov", {}).get("regime", "?")].add(pnl)
            by_vol_regime[quant.get("garch", {}).get("vol_regime", "?")].add(pnl)
            by_conviction[
                self._conviction_bucket(float(committee.get("conviction", 0.0)))
            ].add(pnl)

        return {
            "window_days": days,
            "decisions_considered": considered,
            "trades_scored": executed,
            "overall": overall.as_dict(),
            "by_symbol": {k: v.as_dict() for k, v in sorted(by_symbol.items())},
            "by_markov_regime": {k: v.as_dict() for k, v in sorted(by_regime.items())},
            "by_vol_regime": {k: v.as_dict() for k, v in sorted(by_vol_regime.items())},
            "by_conviction": {k: v.as_dict() for k, v in sorted(by_conviction.items())},
        }

    @staticmethod
    def format(review: dict[str, Any]) -> str:
        o = review["overall"]
        lines = [
            f"== Reflection · last {review['window_days']}d ==",
            (f"{review['decisions_considered']} decisions, "
             f"{review['trades_scored']} trades scored"),
            (f"P&L {o['total_pnl']:+.2f} · win rate {o['win_rate']:.0%} · "
             f"avg {o['avg_pnl']:+.2f}"),
        ]
        for title, key in (("By symbol", "by_symbol"),
                           ("By Markov regime", "by_markov_regime"),
                           ("By vol regime", "by_vol_regime"),
                           ("By conviction", "by_conviction")):
            group = review[key]
            if not group:
                continue
            lines.append(f"\n{title}:")
            for name, stats in group.items():
                lines.append(f"  {name:<12} n={stats['trades']:<3} "
                             f"win={stats['win_rate']:.0%} "
                             f"pnl={stats['total_pnl']:+.2f}")
        return "\n".join(lines)
