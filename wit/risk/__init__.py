"""Deterministic consensus gate + position sizing. No LLM, no broker.

Phase N4 — ``adaptive.py`` ported verbatim (pure math, no broker coupling).
``sizing.py`` ported with gate ordering, thresholds and blocked-reason strings
unchanged — the risk guarantee this port must not move — but its unit system
adapts from MT5's lots/points to Nautilus's raw quantity/price, via the new
``instrument_spec.py`` (``InstrumentSpec``, replacing ``SymbolSpec``) and
``account.py`` (``AccountSnapshot``, replacing ``AccountInfo``). See each
module's docstring for exactly what changed and why.
"""
