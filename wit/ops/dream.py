"""Dream cycle — the fund's weekly self-review.

The state layer (``DreamState``/``Lesson``/``LessonScore``/``load``/``save``)
landed in Phase N2 because ``wit/desks/quant_analyst.py`` embeds the latest
``DreamState`` in the committee's prompt context (one more prior the PM
weighs, never a parameter this loop can change itself). This module now adds
the orchestration half (Phase N7): ``run()`` (the weekly LLM call, wired to
``Reflection``/``Journal``) and ``format_digest()``.

Two guardrails make this safe to run on a watchlist this small (ported
verbatim from ``Wit-Hedge-fund/engine/dream.py``):

  1. Hard per-bucket sample floor, enforced in code. ``_qualifying_buckets``
     filters Reflection's breakdown down to buckets with enough trades
     *before* the LLM ever sees the data, and the LLM is not trusted to
     report trade counts/win rates itself — it only names which bucket a
     lesson is about; the real numbers are filled in here. A lesson naming a
     bucket it was never shown is dropped, not saved.
  2. Lesson efficacy tracking. Every lesson is scoped to exactly one
     ``(dimension, key)`` bucket and gets a stable id, so the *next* dream
     cycle can look up that same bucket in the newly computed Reflection
     breakdown and report, factually, what happened since.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from wit.config import CONFIG, DreamConfig
from wit.ops.reflection import Reflection

if TYPE_CHECKING:
    from wit.ops.journal import Journal

_DIMENSIONS = ("symbol", "markov_regime", "vol_regime", "conviction")


@dataclass(frozen=True)
class Lesson:
    lesson_id: str          # code-generated — never trust the model for identity
    lesson: str
    dimension: str           # one of ("symbol", "markov_regime", "vol_regime", "conviction")
    key: str                 # the exact bucket, e.g. "NVDA", "Bear", "storm", "0.3-0.6"
    confidence: str          # "low" | "medium" | "high"
    basis_trades: int        # Reflection's own numbers, filled in there — not the LLM's
    basis_win_rate: float
    basis_avg_pnl: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LessonScore:
    """How a previous cycle's lesson held up against what actually happened
    in its bucket since. ``trades_since=0`` yields ``None`` rates/pnl rather
    than 0.0 — an empty bucket is not the same as a bucket that broke even."""

    lesson_id: str
    lesson: str
    dimension: str
    key: str
    basis_trades: int
    basis_win_rate: float
    trades_since: int
    win_rate_since: float | None
    pnl_since: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DreamState:
    dream_id: str
    generated_at: str        # ISO timestamp
    window_days: int
    decisions_considered: int
    trades_scored: int
    lessons: list[Lesson] = field(default_factory=list)
    scores: list[LessonScore] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dream_id": self.dream_id,
            "generated_at": self.generated_at,
            "window_days": self.window_days,
            "decisions_considered": self.decisions_considered,
            "trades_scored": self.trades_scored,
            "lessons": [l.to_dict() for l in self.lessons],
            "scores": [s.to_dict() for s in self.scores],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DreamState:
        return cls(
            dream_id=d.get("dream_id", ""),
            generated_at=d.get("generated_at", ""),
            window_days=d.get("window_days", 0),
            decisions_considered=d.get("decisions_considered", 0),
            trades_scored=d.get("trades_scored", 0),
            lessons=[Lesson(**l) for l in d.get("lessons", [])],
            scores=[LessonScore(**s) for s in d.get("scores", [])],
        )

    @classmethod
    def empty(cls) -> DreamState:
        return cls(dream_id="", generated_at="", window_days=0,
                   decisions_considered=0, trades_scored=0)

    def as_prompt_block(self) -> str:
        """Empty when there are no lessons, so QuantAnalystReport can skip
        the section entirely rather than print a header for nothing."""
        if not self.lessons:
            return ""
        header = (f"SELF-REVIEW DESK (trailing {self.window_days}d, "
                  f"{self.trades_scored} trades scored)")
        lines = [
            f"  - [{l.confidence}] {l.lesson} "
            f"(basis: {l.dimension}={l.key}, {l.basis_trades} trades, "
            f"{l.basis_win_rate:.0%} win rate)"
            for l in self.lessons
        ]
        return header + "\n" + "\n".join(lines)


def load(path: str | Path | None = None) -> DreamState:
    """A missing or corrupt state file must never block trading."""
    path = Path(path or CONFIG.dream.state_path)
    if not path.exists():
        return DreamState.empty()
    try:
        return DreamState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - a bad state file degrades, never blocks
        return DreamState.empty()


def save(state: DreamState, path: str | Path | None = None) -> None:
    path = Path(path or CONFIG.dream.state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


# ── Guardrails ───────────────────────────────────────────────────────────


def _qualifying_buckets(review: dict[str, Any], min_trades: int) -> dict[str, dict[str, dict]]:
    """The hard sample floor: only buckets with enough trades to say
    anything meaningful ever reach the LLM. A dimension with nothing
    qualifying is omitted entirely rather than sent as an empty dict."""
    out: dict[str, dict[str, dict]] = {}
    for dim in _DIMENSIONS:
        buckets = review.get(f"by_{dim}", {})
        qualifying = {k: v for k, v in buckets.items() if v.get("trades", 0) >= min_trades}
        if qualifying:
            out[dim] = qualifying
    return out


def _score_previous_lessons(previous: DreamState, review: dict[str, Any]) -> list[LessonScore]:
    """Look up each of the previous cycle's lessons in the newly computed
    review — no extra journal scanning needed, this cycle already computed
    the numbers for its own window."""
    scores = []
    for lesson in previous.lessons:
        bucket = review.get(f"by_{lesson.dimension}", {}).get(lesson.key)
        trades_since = bucket.get("trades", 0) if bucket else 0
        scores.append(LessonScore(
            lesson_id=lesson.lesson_id, lesson=lesson.lesson,
            dimension=lesson.dimension, key=lesson.key,
            basis_trades=lesson.basis_trades, basis_win_rate=lesson.basis_win_rate,
            trades_since=trades_since,
            win_rate_since=bucket["win_rate"] if trades_since else None,
            pnl_since=bucket["total_pnl"] if trades_since else None,
        ))
    return scores


def _build_lessons(raw: list[dict[str, Any]], qualifying: dict[str, dict[str, dict]]) -> list[Lesson]:
    """Turn the LLM's raw ``{lesson, dimension, key, confidence}`` dicts into
    validated ``Lesson``s. A lesson naming a bucket outside ``qualifying`` —
    one it was never shown, i.e. a hallucinated key — is dropped rather than
    trusted."""
    lessons = []
    for item in raw:
        dim, key = item.get("dimension"), item.get("key")
        bucket = qualifying.get(dim, {}).get(key)
        if bucket is None:
            continue
        lessons.append(Lesson(
            lesson_id=uuid.uuid4().hex[:8],
            lesson=str(item.get("lesson", "")).strip(),
            dimension=dim, key=key,
            confidence=item.get("confidence", "low"),
            basis_trades=bucket["trades"], basis_win_rate=bucket["win_rate"],
            basis_avg_pnl=bucket["avg_pnl"],
        ))
    return lessons


# ── Orchestration ────────────────────────────────────────────────────────


class DreamCommittee(Protocol):
    def dream(
        self, qualifying: dict[str, dict[str, dict]], scores: list[dict[str, Any]],
        window_days: int, min_bucket_trades: int,
    ) -> list[dict[str, Any]]: ...


def run(
    committee: DreamCommittee, journal: Journal,
    cfg: DreamConfig | None = None, now: datetime | None = None,
) -> DreamState:
    """One weekly self-review. Never raises internally - every step already
    degrades gracefully on its own (empty buckets, a bad LLM call, a missing
    prior state) - but the caller (``FundStateActor``'s weekly timer) still
    wraps this defensively, matching the MT5 build's scheduler posture.

    No ``cache`` argument (Phase N7 audit finding C1 - this function used to
    take one, to build a ``pnl_by_position`` dict keyed by Nautilus
    ``position_id``, which is not a trade identifier under
    ``OmsType.NETTING``, the only OMS this system runs; see
    ``wit/ops/reflection.py``'s module docstring for the full story).
    ``Reflection.review()`` now reads realized P&L straight from the journal.

    ``now`` must be the caller's own clock, not a bare ``datetime.now(UTC)``
    default (the Phase N5 audit's F1/F3 class of bug: wall-clock "now" is
    months away from the bars actually being processed in a backtest).
    ``FundStateActor``'s weekly timer passes ``self.clock.utc_now()``; the
    default here only exists for a manual/CLI invocation with no clock of
    its own.
    """
    cfg = cfg or CONFIG.dream
    now = now or datetime.now(UTC)
    previous = load(cfg.state_path)

    review = Reflection(journal).review(days=cfg.window_days, now=now)

    scores = _score_previous_lessons(previous, review) if previous.lessons else []
    qualifying = _qualifying_buckets(review, cfg.min_bucket_trades)

    if qualifying:
        raw = committee.dream(
            qualifying, [s.to_dict() for s in scores],
            cfg.window_days, cfg.min_bucket_trades,
        )
        lessons = _build_lessons(raw, qualifying)
    else:
        lessons = []

    state = DreamState(
        dream_id=uuid.uuid4().hex[:12],
        generated_at=now.isoformat(),
        window_days=cfg.window_days,
        decisions_considered=review["decisions_considered"],
        trades_scored=review["trades_scored"],
        lessons=lessons,
        scores=scores,
    )
    save(state, cfg.state_path)
    journal.log_event(
        "dream_cycle",
        f"{len(lessons)} lesson(s), {len(scores)} prior lesson(s) scored",
        ts=now, **state.to_dict(),
    )
    return state


# ── Telegram digest ──────────────────────────────────────────────────────


def format_digest(state: DreamState) -> str:
    head = f"== Dream cycle · {state.generated_at[:10]} · trailing {state.window_days}d =="
    lines = [head, (f"{state.trades_scored} trade(s) scored, "
                    f"{state.decisions_considered} decision(s) considered")]

    if state.scores:
        lines.append("\nLast cycle's lessons, checked against what happened since:")
        for s in state.scores:
            since_txt = (f"{s.trades_since} trades, {s.win_rate_since:.0%} win rate"
                        if s.trades_since else "no trades in this bucket since")
            lines.append(f"  - \"{s.lesson}\" -> {since_txt}")

    if state.lessons:
        lines.append("\nNew lessons:")
        for l in state.lessons:
            lines.append(
                f"  [{l.confidence}] {l.lesson}\n"
                f"    ({l.dimension}={l.key}, {l.basis_trades} trades, "
                f"{l.basis_win_rate:.0%} win rate)"
            )
    else:
        lines.append("\nNo new lessons this cycle — no bucket had enough "
                     "trades to say anything meaningful.")
    return "\n".join(lines)
