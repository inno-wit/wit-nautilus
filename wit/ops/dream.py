"""Dream cycle state — the fund's weekly self-review (state layer only, Phase N2).

``DreamState``/``Lesson``/``LessonScore``/``load``/``save`` are pulled forward
from Phase N7 because ``wit/desks/quant_analyst.py`` embeds the latest
``DreamState`` in the committee's prompt context (one more prior the PM weighs,
never a parameter this loop can change itself). The orchestration half of the
original module (``run``, ``format_digest``, the LLM call, journal/reflection
wiring) lands in Phase N7 on top of this file.

Ported from ``Wit-Hedge-fund/engine/dream.py``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wit.config import CONFIG


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
