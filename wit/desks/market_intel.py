"""Market intelligence desk — real fundamentals and news, not just price action.

Sources, in order of how much they can tell us:
  - yfinance      (always on, no key): sector/PE/market-cap for equities, plus
    real news headlines for every instrument class — FX, metals and indices
    all have Yahoo Finance news feeds (DXY/rates commentary, gold, S&P).
  - Finnhub       (optional, needs FINNHUB_API_KEY): company news + analyst
    recommendation trends, equities only. Skipped silently without a key.
  - Alpha Vantage (optional, needs ALPHAVANTAGE_API_KEY): fundamentals-only
    fallback for whichever of sector/industry/pe_ratio/market_cap yfinance
    didn't return, equities only. Slots into this exact optional/never-raises/
    cached pattern like Finnhub does — the only difference is a hard daily-call
    budget (``AlphaVantageConfig.max_calls_per_day``, default 25 = the free
    tier's actual documented ceiling, confirmed against the broker swap's own
    Alpha Vantage entitlement note): unlike Finnhub, this API has no headroom
    to spend carelessly across a multi-symbol watchlist, so a day-scoped
    counter refuses calls past the budget rather than risking a 429 mid-cycle.

This desk is enrichment, not a hard input — a Yahoo/Finnhub/Alpha Vantage
outage must not stall the cycle, so ``compute`` never raises; a failure just
yields an empty block the committee prompt quietly omits.

Ported verbatim from ``Wit-Hedge-fund/engine/signals/market_intel.py`` (Phase N2).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from wit.config import CONFIG, AlphaVantageConfig, IntelConfig

# MT5-era symbol -> Yahoo Finance ticker, for the instrument classes that need
# translating. Anything not listed here is assumed to be a straight-through
# equity ticker.
_YF_OVERRIDES: dict[str, str] = {
    "XAUUSD": "GC=F", "XAGUSD": "SI=F", "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    "US500": "^GSPC", "US30": "^DJI", "USTEC": "^IXIC",
    "UK100": "^FTSE", "JPN225": "^N225", "AUS200": "^AXJO",
}
_FOREX_LEN = 6


def yf_ticker_for(symbol: str) -> tuple[str, bool]:
    """Return (yahoo ticker, is_equity)."""
    if symbol in _YF_OVERRIDES:
        return _YF_OVERRIDES[symbol], False
    if len(symbol) == _FOREX_LEN and symbol.isalpha() and symbol.isupper():
        return f"{symbol}=X", False
    return symbol, True


@dataclass(frozen=True)
class MarketIntel:
    symbol: str
    is_equity: bool
    sector: str | None = None
    industry: str | None = None
    pe_ratio: float | None = None
    market_cap: float | None = None
    headlines: list[str] = field(default_factory=list)
    analyst_summary: str | None = None   # e.g. "buy 22 / hold 9 / sell 1"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_content(self) -> bool:
        return bool(self.sector or self.headlines or self.analyst_summary)

    def as_prompt_block(self) -> str:
        lines: list[str] = []
        if self.is_equity and (self.sector or self.pe_ratio or self.market_cap):
            fundamentals = []
            if self.sector:
                fundamentals.append(f"{self.sector}" + (f" / {self.industry}" if self.industry else ""))
            if self.pe_ratio:
                fundamentals.append(f"P/E {self.pe_ratio:.1f}")
            if self.market_cap:
                fundamentals.append(f"market cap ${self.market_cap / 1e9:.1f}B")
            lines.append("Fundamentals: " + ", ".join(fundamentals))
        if self.analyst_summary:
            lines.append(f"Analyst recommendations: {self.analyst_summary}")
        if self.headlines:
            lines.append("Recent headlines:")
            lines += [f"  - {h}" for h in self.headlines]
        return "\n".join(lines) if lines else "(no market intelligence available)"


# ── yfinance ─────────────────────────────────────────────────────────────

def _yf_fetch(yf_ticker: str, is_equity: bool, news_count: int) -> dict[str, Any]:
    import yfinance as yf

    t = yf.Ticker(yf_ticker)
    out: dict[str, Any] = {}
    if is_equity:
        info = t.get_info() or {}
        out["sector"] = info.get("sector")
        out["industry"] = info.get("industry")
        out["pe_ratio"] = info.get("trailingPE")
        out["market_cap"] = info.get("marketCap")

    headlines = []
    for item in t.get_news(count=news_count) or []:
        content = item.get("content", item)
        title = content.get("title")
        if title:
            headlines.append(title)
    out["headlines"] = headlines
    return out


# ── Finnhub (optional) ───────────────────────────────────────────────────

def _finnhub_get(path: str, api_key: str, **params: str) -> Any:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://finnhub.io/api/v1/{path}?{query}&token={api_key}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


_FINNHUB_ERRORS = (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError,
                   KeyError, IndexError, TypeError)


def _finnhub_fetch(symbol: str, api_key: str) -> dict[str, Any]:
    """Each Finnhub endpoint is fetched independently: recommendation-trend can be
    premium-gated and answer with a 200 carrying marketing HTML instead of a clean
    4xx, which fails JSON parsing. That must not cost us the free company-news
    headlines."""
    out: dict[str, Any] = {}
    errors: list[str] = []

    try:
        today = date.today()  # noqa: DTZ011 - date-only range param, tz doesn't matter here
        news = _finnhub_get(
            "company-news", api_key, symbol=symbol,
            **{"from": str(today - timedelta(days=5)), "to": str(today)},
        )
        out["headlines"] = [n["headline"] for n in news[:5] if n.get("headline")]
    except _FINNHUB_ERRORS as e:
        errors.append(f"company-news: {type(e).__name__}: {e}")

    try:
        trends = _finnhub_get("stock/recommendation-trend", api_key, symbol=symbol)
        if trends:
            latest = trends[0]
            out["analyst_summary"] = (
                f"buy {latest.get('buy', 0)} / hold {latest.get('hold', 0)} / "
                f"sell {latest.get('sell', 0)} (strong buy {latest.get('strongBuy', 0)}, "
                f"strong sell {latest.get('strongSell', 0)})"
            )
    except _FINNHUB_ERRORS as e:
        errors.append(f"recommendation-trend: {type(e).__name__}: {e}")

    if errors:
        out["_errors"] = errors
    return out


# ── Alpha Vantage (optional, fundamentals-only fallback, hard daily budget) ──

class _DailyCallBudget:
    """A day-scoped call counter, not a rolling window — Alpha Vantage's free
    tier resets at UTC midnight, and a simple per-day count is what
    ``AlphaVantageConfig.max_calls_per_day`` is actually meant to cap (unlike
    Polygon's rolling-minute limit in ``wit/adapters/polygon/data.py``, which
    is a different kind of ceiling on a different provider)."""

    def __init__(self) -> None:
        self._day: date = date.today()  # noqa: DTZ011 - day-granularity budget, tz doesn't matter here
        self._used = 0

    def try_consume(self, max_calls: int) -> bool:
        today = date.today()  # noqa: DTZ011 - day-granularity budget, tz doesn't matter here
        if today != self._day:
            self._day, self._used = today, 0
        if self._used >= max_calls:
            return False
        self._used += 1
        return True


_av_budget = _DailyCallBudget()


def _alphavantage_get(function: str, api_key: str, **params: str) -> Any:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://www.alphavantage.co/query?function={function}&{query}&apikey={api_key}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


_ALPHAVANTAGE_ERRORS = (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError,
                        KeyError, ValueError, TypeError)


def _alphavantage_fetch(symbol: str, api_key: str) -> dict[str, Any]:
    """Fundamentals only (the ``OVERVIEW`` endpoint) — mirrors ``_finnhub_fetch``'s
    never-raises-past-this-function shape, but has exactly one endpoint to call
    (no per-endpoint try/except needed) since the daily budget makes spending a
    second call per symbol on headlines/analyst data (which yfinance/Finnhub
    already cover) not worth it."""
    out: dict[str, Any] = {}
    try:
        overview = _alphavantage_get("OVERVIEW", api_key, symbol=symbol)
        if overview.get("Sector"):
            out["sector"] = overview["Sector"]
        if overview.get("Industry"):
            out["industry"] = overview["Industry"]
        pe = overview.get("PERatio")
        if pe not in (None, "None", "-"):
            out["pe_ratio"] = float(pe)
        cap = overview.get("MarketCapitalization")
        if cap not in (None, "None", "-"):
            out["market_cap"] = float(cap)
    except _ALPHAVANTAGE_ERRORS as e:
        out["_errors"] = [f"overview: {type(e).__name__}: {e}"]
    return out


# ── Cache (a live cycle re-reads the same symbol at most once per TTL) ────

_cache: dict[str, tuple[float, MarketIntel]] = {}


def compute(
    symbol: str, cfg: IntelConfig | None = None, av_cfg: AlphaVantageConfig | None = None,
) -> MarketIntel:
    """Fetch fundamentals + news for ``symbol``. Never raises."""
    cfg = cfg or CONFIG.intel
    av_cfg = av_cfg or CONFIG.alphavantage
    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < cfg.cache_ttl_seconds:
        return cached[1]

    yf_ticker, is_equity = yf_ticker_for(symbol)
    data: dict[str, Any] = {}
    error: str | None = None

    try:
        data.update(_yf_fetch(yf_ticker, is_equity, cfg.news_count))
    except Exception as e:  # noqa: BLE001 - enrichment must never break the cycle
        error = f"yfinance: {type(e).__name__}: {e}"

    if is_equity and cfg.finnhub_api_key:
        try:
            fh = _finnhub_fetch(symbol, cfg.finnhub_api_key)
        except Exception as e:  # noqa: BLE001 - belt-and-braces on top of
                                 # _finnhub_fetch's own per-endpoint handling
            fh, e_note = {}, f"finnhub: {type(e).__name__}: {e}"
            error = (error + "; " if error else "") + e_note
        else:
            fh_errors = fh.pop("_errors", None)
            if fh_errors:
                note = "finnhub: " + "; ".join(fh_errors)
                error = (error + "; " if error else "") + note
        # Finnhub's headlines are fresher/more targeted; prepend and dedup.
        merged = fh.pop("headlines", []) + data.get("headlines", [])
        seen: set[str] = set()
        data["headlines"] = [h for h in merged if not (h in seen or seen.add(h))]
        data.update(fh)

    # Alpha Vantage: fundamentals-only fallback for whatever yfinance/Finnhub
    # left empty (never overwrites a value they already found) - budget-checked
    # BEFORE the call, not after, so a symbol that's already fully covered
    # never spends from the 25/day ceiling at all.
    missing_fundamentals = is_equity and any(
        data.get(key) is None for key in ("sector", "industry", "pe_ratio", "market_cap")
    )
    if missing_fundamentals and av_cfg.api_key and _av_budget.try_consume(av_cfg.max_calls_per_day):
        try:
            av = _alphavantage_fetch(symbol, av_cfg.api_key)
        except Exception as e:  # noqa: BLE001 - belt-and-braces on top of
                                 # _alphavantage_fetch's own try/except
            av, e_note = {}, f"alphavantage: {type(e).__name__}: {e}"
            error = (error + "; " if error else "") + e_note
        else:
            av_errors = av.pop("_errors", None)
            if av_errors:
                note = "alphavantage: " + "; ".join(av_errors)
                error = (error + "; " if error else "") + note
        for key in ("sector", "industry", "pe_ratio", "market_cap"):
            if data.get(key) is None and av.get(key) is not None:
                data[key] = av[key]

    intel = MarketIntel(
        symbol=symbol, is_equity=is_equity,
        sector=data.get("sector"), industry=data.get("industry"),
        pe_ratio=data.get("pe_ratio"), market_cap=data.get("market_cap"),
        headlines=data.get("headlines", [])[:cfg.news_count],
        analyst_summary=data.get("analyst_summary"),
        error=error,
    )
    _cache[symbol] = (now, intel)
    return intel


def prefetch(symbols: Iterable[str], cfg: IntelConfig | None = None,
             max_workers: int = 8) -> None:
    """Warm the intel cache for ``symbols`` concurrently. Pure latency win, not a
    freshness trade — data is still fetched this cycle, only in parallel."""
    cfg = cfg or CONFIG.intel
    syms = list(dict.fromkeys(symbols))  # de-dup, preserve order
    if len(syms) <= 1:
        if syms:
            compute(syms[0], cfg)
        return
    with ThreadPoolExecutor(max_workers=min(max_workers, len(syms))) as pool:
        for _ in pool.map(lambda s: compute(s, cfg), syms):
            pass
