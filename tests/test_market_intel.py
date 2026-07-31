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


# ── Alpha Vantage: daily call budget ─────────────────────────────────────

def test_daily_call_budget_allows_up_to_the_max():
    budget = mi._DailyCallBudget()
    assert all(budget.try_consume(max_calls=3) for _ in range(3))


def test_daily_call_budget_refuses_past_the_max():
    budget = mi._DailyCallBudget()
    for _ in range(3):
        assert budget.try_consume(max_calls=3) is True
    assert budget.try_consume(max_calls=3) is False


def test_daily_call_budget_resets_on_a_new_day(monkeypatch):
    from datetime import date, timedelta

    budget = mi._DailyCallBudget()
    assert budget.try_consume(max_calls=1) is True
    assert budget.try_consume(max_calls=1) is False  # exhausted today

    tomorrow = date.today() + timedelta(days=1)  # noqa: DTZ011 - test setup, tz doesn't matter here

    class _FakeDate(date):
        @classmethod
        def today(cls):
            return tomorrow

    monkeypatch.setattr(mi, "date", _FakeDate)
    assert budget.try_consume(max_calls=1) is True  # budget reset for the new day


# ── Alpha Vantage: fetch (fundamentals-only, never raises past this function) ─

def test_alphavantage_fetch_extracts_sector_industry_pe_and_market_cap(monkeypatch):
    monkeypatch.setattr(mi, "_alphavantage_get", lambda function, api_key, **params: {
        "Sector": "TECHNOLOGY", "Industry": "SEMICONDUCTORS",
        "PERatio": "45.2", "MarketCapitalization": "3200000000000",
    })
    out = mi._alphavantage_fetch("NVDA", "fake-key")
    assert out["sector"] == "TECHNOLOGY"
    assert out["industry"] == "SEMICONDUCTORS"
    assert out["pe_ratio"] == 45.2
    assert out["market_cap"] == 3200000000000.0
    assert "_errors" not in out


def test_alphavantage_fetch_treats_none_placeholder_values_as_missing(monkeypatch):
    """Alpha Vantage's OVERVIEW endpoint returns the literal string "None" for
    fields it doesn't have, not a JSON null - must not be parsed as a real PE
    ratio of the string "None"."""
    monkeypatch.setattr(mi, "_alphavantage_get", lambda function, api_key, **params: {
        "Sector": "TECHNOLOGY", "PERatio": "None", "MarketCapitalization": "-",
    })
    out = mi._alphavantage_fetch("NVDA", "fake-key")
    assert out["sector"] == "TECHNOLOGY"
    assert "pe_ratio" not in out
    assert "market_cap" not in out


def test_alphavantage_fetch_never_raises_on_a_network_error(monkeypatch):
    import urllib.error

    def _boom(*a, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(mi, "_alphavantage_get", _boom)
    out = mi._alphavantage_fetch("NVDA", "fake-key")
    assert "_errors" in out


# ── Alpha Vantage: wired into compute() as a budget-gated fallback ────────

def test_compute_skips_alphavantage_when_yfinance_already_has_fundamentals(monkeypatch):
    from wit.config import AlphaVantageConfig, IntelConfig

    monkeypatch.setattr(mi, "_yf_fetch", lambda *a, **kw: {
        "sector": "TECHNOLOGY", "industry": "SEMICONDUCTORS",
        "pe_ratio": 40.0, "market_cap": 1e12, "headlines": [],
    })
    called = []
    monkeypatch.setattr(mi, "_alphavantage_fetch", lambda *a, **kw: called.append(1) or {})
    mi._cache.clear()
    mi.compute("NVDA", cfg=IntelConfig(finnhub_api_key=""),
               av_cfg=AlphaVantageConfig(api_key="fake-key", max_calls_per_day=25))
    assert called == []  # budget never even touched - nothing was missing


def test_compute_calls_alphavantage_when_fundamentals_are_missing(monkeypatch):
    from wit.config import AlphaVantageConfig, IntelConfig

    monkeypatch.setattr(mi, "_yf_fetch", lambda *a, **kw: {"headlines": []})
    called = []
    monkeypatch.setattr(mi, "_alphavantage_fetch", lambda symbol, api_key: (
        called.append(symbol) or {"sector": "TECHNOLOGY", "pe_ratio": 40.0}
    ))
    mi._cache.clear()
    intel = mi.compute("NVDA", cfg=IntelConfig(finnhub_api_key=""),
                       av_cfg=AlphaVantageConfig(api_key="fake-key", max_calls_per_day=25))
    assert called == ["NVDA"]
    assert intel.sector == "TECHNOLOGY"
    assert intel.pe_ratio == 40.0


def test_compute_never_calls_alphavantage_without_an_api_key(monkeypatch):
    from wit.config import AlphaVantageConfig, IntelConfig

    monkeypatch.setattr(mi, "_yf_fetch", lambda *a, **kw: {"headlines": []})
    called = []
    monkeypatch.setattr(mi, "_alphavantage_fetch", lambda *a, **kw: called.append(1) or {})
    mi._cache.clear()
    mi.compute("NVDA", cfg=IntelConfig(finnhub_api_key=""),
               av_cfg=AlphaVantageConfig(api_key="", max_calls_per_day=25))
    assert called == []


def test_compute_respects_the_daily_budget_across_symbols(monkeypatch):
    """The 25/day ceiling is a hard budget, not a soft target - once
    exhausted, later symbols in the same cycle must be skipped, not queued
    or retried."""
    from wit.config import AlphaVantageConfig, IntelConfig

    monkeypatch.setattr(mi, "_yf_fetch", lambda *a, **kw: {"headlines": []})
    called = []
    monkeypatch.setattr(mi, "_alphavantage_fetch", lambda symbol, api_key: (
        called.append(symbol) or {}
    ))
    mi._cache.clear()
    mi._av_budget = mi._DailyCallBudget()
    av_cfg = AlphaVantageConfig(api_key="fake-key", max_calls_per_day=1)
    intel_cfg = IntelConfig(finnhub_api_key="")
    mi.compute("NVDA", cfg=intel_cfg, av_cfg=av_cfg)
    mi.compute("AAPL", cfg=intel_cfg, av_cfg=av_cfg)
    assert called == ["NVDA"]  # AAPL's call was refused by the exhausted budget


def test_compute_never_raises_on_an_alphavantage_error(monkeypatch):
    from wit.config import AlphaVantageConfig, IntelConfig

    monkeypatch.setattr(mi, "_yf_fetch", lambda *a, **kw: {"headlines": []})

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(mi, "_alphavantage_fetch", _boom)
    mi._cache.clear()
    intel = mi.compute("NVDA", cfg=IntelConfig(finnhub_api_key=""),
                       av_cfg=AlphaVantageConfig(api_key="fake-key", max_calls_per_day=25))
    assert intel.error is not None and "alphavantage" in intel.error
