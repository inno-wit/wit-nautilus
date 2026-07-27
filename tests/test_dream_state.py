"""Direct coverage for wit/ops/dream.py's state layer, pulled forward into
Phase N2 alongside its tests (Phase N2 audit finding F4). Ported from
Wit-Hedge-fund/tests/test_dream.py's "State persistence" and prompt-block
sections — the ``run``/``format_digest`` orchestration tests stay with that
half of the module in Phase N7.

``dream.load()``'s docstring makes an explicit safety claim — "a missing or
corrupt state file must never block trading" — implemented as a bare
``except Exception``. These pin that contract so a future narrowing of the
except clause (or a schema change that makes ``Lesson(**l)`` raise on an old
state file) fails loudly here instead of at startup in production.
"""
from __future__ import annotations

import json

from wit.ops import dream


def test_load_missing_file_is_empty(tmp_path):
    state = dream.load(tmp_path / "nope.json")
    assert state.lessons == [] and state.scores == []


def test_load_corrupt_file_is_empty_not_raising(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert dream.load(path).lessons == []


def test_save_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = dream.DreamState(
        dream_id="d1", generated_at="2026-07-01T00:00:00", window_days=30,
        decisions_considered=10, trades_scored=8,
        lessons=[dream.Lesson("l1", "x", "symbol", "NVDA", "low", 8, 0.2, -5.0)],
        scores=[dream.LessonScore("l0", "y", "symbol", "TSLA", 5, 0.4, 6, 0.5, 12.0)],
    )
    dream.save(state, path)
    reloaded = dream.load(path)
    assert reloaded == state
    assert json.loads(path.read_text(encoding="utf-8"))["lessons"][0]["lesson_id"] == "l1"


def test_as_prompt_block_empty_when_no_lessons():
    assert dream.DreamState.empty().as_prompt_block() == ""


def test_as_prompt_block_lists_lessons_with_basis():
    state = dream.DreamState(
        dream_id="d1", generated_at="2026-07-01T00:00:00", window_days=30,
        decisions_considered=10, trades_scored=8,
        lessons=[dream.Lesson("l1", "NVDA BUYs underperform in storm vol", "symbol",
                              "NVDA", "medium", 8, 0.25, -12.0)],
    )
    block = state.as_prompt_block()
    assert "SELF-REVIEW DESK" in block
    assert "NVDA BUYs underperform in storm vol" in block
    assert "8 trades" in block and "25% win rate" in block
