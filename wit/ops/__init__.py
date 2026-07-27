"""Record-keeping and safety: journal, reflection, dream cycle, alerts, market hours, safety.

Phase N2 (``market_hours.py``, ``prefilter.py``, and ``dream.py``'s state layer —
``DreamState``/``Lesson``/``LessonScore``/``load``/``save`` — all pulled forward because the
Phase N2 desks (``quant_analyst.py``) and gates need them; see each module's docstring for
the exact dependency that forced the move). ``journal.py`` landed in Phase N5 (``WitStrategy``
needed it to log decisions). Phase N7 adds ``reflection.py``/``alerts.py`` and ``dream.py``'s
orchestration half (``run``/``format_digest``). ``safety.py`` never landed as its own module -
it moved into ``FundStateActor`` (the kill switch, daily-loss breaker) plus the boot-time
``assert_paper_only`` in Phase N5/N6 instead. Ported near-verbatim from ``Wit-Hedge-fund/engine/``;
see the build plan §1 mapping table for what changes and why.
"""
