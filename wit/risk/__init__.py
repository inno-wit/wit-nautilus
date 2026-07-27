"""Deterministic consensus gate + position sizing. No LLM, no broker.

Phase N4 — ``sizing.py``/``adaptive.py`` ported near-verbatim from
``Wit-Hedge-fund/engine/risk/``; the only substantive change is a new ``instrument_spec.py``
shim turning a Nautilus ``Instrument`` into the same spec shape ``build_plan`` already
consumes. ``build_plan`` itself must not change — see the build plan §1.3/§1.4.
"""
