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
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    ib: IBConfig = field(default_factory=IBConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)

    journal_path: str = str(PROJECT_ROOT / "data" / "journal.jsonl")
    dream_state_path: str = str(PROJECT_ROOT / "data" / "dream_state.json")
    decision_cache_path: str = str(PROJECT_ROOT / "data" / "decisions.db")


CONFIG = Config()
