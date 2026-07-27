"""Concurrent intel prefetch (wit/desks/market_intel.py), ported from
Wit-Hedge-fund/tests/test_phase9.py's prefetch section — pure fan-out logic,
network calls are monkeypatched out.
"""
from __future__ import annotations

from wit.desks import market_intel as mi


def test_prefetch_warms_each_unique_symbol_once(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(mi, "compute",
                        lambda symbol, cfg=None: seen.append(symbol) or object())
    mi.prefetch(["EURUSD", "NVDA", "EURUSD"])   # duplicate collapses
    assert sorted(seen) == ["EURUSD", "NVDA"]


def test_prefetch_handles_the_single_symbol_case(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(mi, "compute",
                        lambda symbol, cfg=None: seen.append(symbol) or object())
    mi.prefetch(["EURUSD"])
    assert seen == ["EURUSD"]
