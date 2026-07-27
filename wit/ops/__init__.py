"""Record-keeping and safety: journal, reflection, dream cycle, alerts, market hours, safety.

Phase N2 (``market_hours.py``, ``prefilter.py``, and ``dream.py``'s state layer —
``DreamState``/``Lesson``/``LessonScore``/``load``/``save`` — all pulled forward because the
Phase N2 desks (``quant_analyst.py``) and gates need them; see each module's docstring for
the exact dependency that forced the move). Phase N7 adds ``journal.py``/``reflection.py``/
``alerts.py`` and ``dream.py``'s orchestration half (``run``/``format_digest``). Phase N5/N6
add ``safety.py``, which moves into ``FundStateActor`` + boot-time assertions. Ported
near-verbatim from ``Wit-Hedge-fund/engine/``; see the build plan §1 mapping table for what
changes and why.
"""
