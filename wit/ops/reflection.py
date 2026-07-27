"""Reflection — score closed trades against the thesis that opened them.

The journal records why each position was taken. Reflection joins that to the
realized outcome so the fund can see *which kinds of reasoning* made money,
not just which trades did. This is the input to the weekly dream cycle and
the daily/weekly Telegram review.

Deliberately statistics-first: it reports edge by symbol, Markov regime, vol
regime and conviction bucket. No LLM is required to produce it.

Ported from ``Wit-Hedge-fund/engine/reflection.py`` (Phase N7). The join key
has changed three times over this port's history:

1. The MT5 build had one broker-side ticket per order, so ``review()`` took
   ``deals_by_ticket: dict[int, float]`` straight from the broker.
2. N7's first cut resolved a decision's ``client_order_id`` to a Nautilus
   ``position_id`` (via the journal's own ``order_filled`` events) and
   looked that up in a caller-supplied ``pnl_by_position`` dict built from
   ``cache.positions_closed()``. **This does not work** (Phase N7 audit
   finding C1, proven by executing a real multi-trade backtest, not by
   inspection): under ``OmsType.NETTING`` - the only OMS this system runs,
   hard-coded by the IBKR execution client - Nautilus derives
   ``position_id`` as the constant ``f"{instrument_id}-{strategy_id}"``, not
   a per-trade identifier, and ``Cache`` evicts a symbol's prior closed
   position from its closed-position index the instant that symbol is
   re-entered. The join either found nothing, or silently scored every
   decision on a symbol against whichever single trade happened to survive.
3. The follow-up fix paired each symbol's executed decisions with its
   ``position_closed`` events chronologically (FIFO per symbol), reasoning
   that ``RiskConfig.per_symbol_max_positions = 1`` guarantees strict
   alternation. **This was closer, but still wrong** (round-8 verification
   audit, again by execution): an order journalled as ``ok`` means
   *submitted*, not *filled* - a rejected or cancelled order has no
   ``position_closed`` event, so it silently ate the next real close's slot
   in the FIFO queue, permanently offsetting every later pairing on that
   symbol. The same FIFO also mis-attributed a trade whose entry fell just
   before a review window and whose close fell just after it, and vice
   versa - not self-correcting, since every later pairing on that symbol
   inherits the same one-slot offset.

This version needs no cache, no chronological assumption, and no ambient
Nautilus ``position_id`` at all - it joins on an **exact id**:
``WitStrategy.on_position_closed`` journals ``opening_order_id``, which
Nautilus sets to the real ``ClientOrderId`` of the specific order that
opened that position (confirmed against the installed
``nautilus_trader/model/events/position.pyx``, not guessed) - the *same* id
``log_decision`` already journals as a completed entry's top-level
``client_order_id``. A decision whose order was rejected/cancelled never
gets a matching close, so it is correctly excluded rather than stealing
another trade's slot; a trade whose entry or close falls outside the review
window is correctly excluded on its own, with no cascading effect on any
other trade. The aggregation logic itself (win rate by
symbol/regime/vol-regime/conviction) is unchanged from the MT5 build.
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


def _pnl_by_opening_order_id(entries: list[dict[str, Any]]) -> dict[str, float]:
    """Realized P&L keyed by the ``ClientOrderId`` of the order that opened
    the position - see this module's docstring for why this, and not
    ``position_id`` or chronological order, is the join key."""
    out: dict[str, float] = {}
    for rec in entries:
        if rec.get("type") != "event" or rec.get("kind") != "position_closed":
            continue
        opening_order_id, pnl = rec.get("opening_order_id"), rec.get("realized_pnl")
        if opening_order_id and pnl is not None:
            out[opening_order_id] = float(pnl)
    return out


@dataclass
class Reflection:
    journal: Journal = field(default_factory=Journal)

    @staticmethod
    def _conviction_bucket(c: float) -> str:
        return "0.0-0.3" if c < 0.3 else "0.3-0.6" if c < 0.6 else "0.6-1.0"

    def review(self, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
        """Join journaled decisions to realized P&L by an exact
        ``client_order_id == opening_order_id`` match (see this module's
        docstring for why).

        ``now`` must be the caller's own clock, not a bare
        ``datetime.now(UTC)`` default (Phase N7 audit finding H1, the same
        N5 F1/F3 bug class): ``FundStateActor``'s daily-timer callback runs
        on simulated time in a backtest, potentially months away from
        wall-clock "now" - without an explicit ``now``, every "last N days"
        window there degenerates to "everything the journal has ever
        recorded", proven by a real multi-day backtest reporting a
        monotonically growing decision count from a nominal 1-day window.
        This only works because every journal write in the live/backtest
        path now stamps ``ts`` from the same clock (``Journal.log_decision``/
        ``log_event``'s ``ts=`` parameter) - passing ``now`` here alone is
        not sufficient if the entries themselves are still wall-clock
        stamped.
        """
        now = now or datetime.now(UTC)
        since = now - timedelta(days=days)
        entries = self.journal.entries_since(since)
        pnl_by_opening_order = _pnl_by_opening_order_id(entries)

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
            if not order.get("ok"):
                continue
            coid = rec.get("client_order_id") or order.get("client_order_id")
            if not coid or coid not in pnl_by_opening_order:
                continue  # submitted but never filled, or its close falls outside this window
            pnl = pnl_by_opening_order[coid]

            executed += 1
            symbol = rec["symbol"]
            quant = rec.get("quant", {})
            committee = rec.get("committee", {})

            overall.add(pnl)
            by_symbol[symbol].add(pnl)
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
