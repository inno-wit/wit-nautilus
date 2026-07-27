"""Central configuration. Loads a gitignored ``.env`` at the repo root.

Mirrors the MT5 build's config pattern (``Wit-Hedge-fund/engine/config.py``): a tiny
``.env`` parser, no third-party dependency required for env loading, typed frozen
dataclasses per concern. Fields here are the ones confirmed by the plan
(``docs`` cross-reference: ``whatif-we-used-alpaca-quirky-aurora.md``); IB and instrument
fields will grow through Phase N4/N6 as Phase N0's findings land.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """Populate ``os.environ`` from a ``.env`` file (does not overwrite existing)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = True) -> bool:
    raw = _env(key, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = _env("ANTHROPIC_API_KEY")
    base_url: str = _env("ANTHROPIC_BASE_URL")  # empty -> Anthropic's default endpoint
    deep_model: str = _env("WIT_DEEP_MODEL")
    quick_model: str = _env("WIT_QUICK_MODEL")
    # Paces LiveCommitteeProvider's bull/bear/PM calls (wit/committee/live.py's
    # _RateLimiter). Direct Anthropic accounts have materially higher limits than the
    # MT5 build's free-tier gateway did, so the default is higher than that build's 10 —
    # override for your account's actual tier. 0 disables pacing entirely.
    rpm_limit: int = int(_env("WIT_LLM_RPM_LIMIT", "50") or "50")


@dataclass(frozen=True)
class IBConfig:
    host: str = _env("IBG_HOST", "127.0.0.1")
    port: int = int(_env("IBG_PORT", "4002") or "4002")  # 4002 paper / 4001 live
    client_id: int = int(_env("IBG_CLIENT_ID", "1") or "1")
    account_id: str = _env("TWS_ACCOUNT")


@dataclass(frozen=True)
class SafetyConfig:
    # Hard lock, asserted at node boot (Phase N6) against the IB account id prefix —
    # never relaxed purely from this flag. See §1.4 of the build plan.
    paper_only: bool = _env_bool("WIT_PAPER_ONLY", True)
    kill_switch_file: str = str(PROJECT_ROOT / "data" / "KILL_SWITCH")


@dataclass(frozen=True)
class IntelConfig:
    """Market intelligence desk (Phase N2, ``wit/desks/market_intel.py``). yfinance
    needs no key; Finnhub is optional."""

    finnhub_api_key: str = _env("FINNHUB_API_KEY")
    news_count: int = 4          # headlines pulled per symbol
    cache_ttl_seconds: int = 900  # avoid hammering Yahoo/Finnhub every cycle


@dataclass(frozen=True)
class RiskConfig:
    """Ported from the MT5 build's ``RiskConfig`` — gate ordering, caps and
    thresholds are the risk guarantees the port must not silently change (see §1.4 of
    the build plan). ``paper_only`` here mirrors ``SafetyConfig.paper_only``; the boot
    assertion in Phase N6 is the actual enforcement point, not this flag.

    ``max_spread_points`` is dropped as of Phase N4: it was an MT5-specific "points"
    concept (a broker/instrument-specific tick count) with no coherent IBKR
    equivalent. ``max_spread_pct`` — already flagged in the MT5 build's own comment
    as the more correct cross-instrument measure — is now the sole spread gate. See
    ``wit/risk/instrument_spec.py``'s module docstring for the full reasoning."""

    risk_per_trade: float = 0.005        # 0.5% of equity risked per trade
    max_concurrent_positions: int = 3
    max_daily_loss: float = 0.03         # 3% equity daily loss -> auto-halt
    per_symbol_max_positions: int = 1
    max_spread_pct: float = 0.0015       # skip if spread wider than 0.15% of price
    target_annual_vol: float = 0.15      # GARCH vol-target anchor
    size_multiplier_floor: float = 0.25  # GARCH clamp
    size_multiplier_cap: float = 2.0
    paper_only: bool = True

    min_conviction: float = 0.15         # block a non-HOLD verdict below this
    cooldown_minutes: int = 60           # throttle re-entry on the same symbol
    max_entry_slippage_pct: float = 0.002  # reject at execution if price drifted
    correlation_groups: dict[str, tuple[str, ...]] = field(default_factory=lambda: {
        "us_tech": ("NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA", "US500"),
    })
    max_positions_per_group: int = 2


@dataclass(frozen=True)
class PrefilterConfig:
    """Deterministic pre-committee gate (``wit/ops/prefilter.py``). OFF by default —
    enabling it changes which symbols ever reach the LLM committee, so it is a
    deliberate, evidence-backed switch validated with ``prefilter.replay`` first."""

    enabled: bool = _env_bool("WIT_PREFILTER", False)
    min_replay_decisions: int = 150   # comparable committee decisions the replay must span
    min_observed_skips: int = 20      # ...of which this many in the flat+neutral state


@dataclass(frozen=True)
class SessionConfig:
    """Per-instrument market-hours awareness (``wit/ops/market_hours.py``). FX/metals/
    index CFDs trade 24/5; individual US equities quote only during the regular US cash
    session. Times are wall-clock America/New_York (DST handled in market_hours.py)."""

    enforce_equity_hours: bool = _env_bool("WIT_EQUITY_HOURS", True)
    cash_open: time = time(9, 30)   # 09:30 ET regular session open
    cash_close: time = time(16, 0)  # 16:00 ET regular session close
    equity_symbols: frozenset[str] = frozenset(
        {"NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA"}
    )


@dataclass(frozen=True)
class AdaptiveConfig:
    """Adaptive position sizing (Phase N4, ``wit/risk/adaptive.py``) — two deterministic
    multipliers layered on the base risk, both 1.0 (no effect) until they apply."""

    drawdown_throttle: bool = _env_bool("WIT_DRAWDOWN_THROTTLE", True)
    drawdown_mult_floor: float = 0.5

    use_fractional_kelly: bool = _env_bool("WIT_KELLY", False)
    kelly_fraction: float = 0.25
    kelly_min_trades: int = 30
    kelly_lookback_days: int = 30
    kelly_mult_floor: float = 0.5
    kelly_mult_cap: float = 1.5


@dataclass(frozen=True)
class DreamConfig:
    """Weekly self-review (Phase N7, ``wit/ops/dream.py``). Informational only."""

    state_path: str = str(PROJECT_ROOT / "data" / "dream_state.json")
    window_days: int = 30
    min_bucket_trades: int = 5


@dataclass(frozen=True)
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    ib: IBConfig = field(default_factory=IBConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    intel: IntelConfig = field(default_factory=IntelConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    prefilter: PrefilterConfig = field(default_factory=PrefilterConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)
    dream: DreamConfig = field(default_factory=DreamConfig)

    watchlist: tuple[str, ...] = (
        "EURUSD", "XAUUSD", "US500",
        "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA",
    )
    timeframe: str = "H1"
    history_bars: int = 750

    journal_path: str = str(PROJECT_ROOT / "data" / "journal.jsonl")
    decision_cache_path: str = str(PROJECT_ROOT / "data" / "decisions.db")


CONFIG = Config()
