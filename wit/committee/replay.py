"""``ReplayCommitteeProvider`` — a SQLite decision cache keyed by
``(instrument_id, bar_ts_ns, model_key, sha256(timeframe + prompt_block))``
(build plan §1.2 / §3 Phase N3).

This is new code, not a port: the MT5 build has no backtester, so nothing
like it existed to port. The cache is what makes a full-fidelity backtest of
an LLM-mediated strategy affordable to run more than once — pay the API bill
once per ``(symbol, bar)`` in ``record`` mode, then replay those exact
decisions for free while sweeping risk/sizing parameters in ``strict`` mode.

Two modes:

- ``record``: cache hit returns the cached decision; cache miss calls the
  wrapped live provider once and writes the result through before returning
  it (unless the call abstained — see below). Used to populate the cache
  from real committee runs.
- ``strict``: cache hit returns the cached decision; cache miss **raises**
  rather than abstaining. A missing entry during a `strict` backtest means
  the cache is incomplete for the run being replayed — silently substituting
  a HOLD would quietly distort the backtest's P&L instead of surfacing the
  gap, which is worse than stopping. This is a deliberate exception to
  ``DecisionProvider.decide``'s general "never raises" contract, which exists
  to keep a live LLM outage from blocking trading; a replay cache miss is an
  operator/setup error, not a live outage, and deserves to fail loudly.

Two things the key must include but the report itself doesn't carry — the
Phase N3 audit's finding F6 caught both missing initially:

- **``model_key``**: a caller-supplied identifier (recommend
  ``f"{deep_model}:{quick_model}"``) for which models produced the recording.
  Nothing about a ``QuantAnalystReport`` reveals which model answered it, so a
  cache recorded under one model and silently replayed after switching models
  would report the wrong model's performance with no warning. Pass the same
  ``model_key`` to both the recording and the replaying provider; a mismatch
  is a cache miss (a mismatch is exactly what you want it to be — loud, not
  silent).
- **``report.timeframe``**: interpolated directly into every prompt sent to
  the model, but not itself part of ``as_prompt_block()``'s output.

Also per finding F4: a ``decide()`` call that abstains (``decision.error is
not None``) is never written to the cache in ``record`` mode. The live
provider's contract is "never raises, abstain instead" — which means a
transient outage during a long recording pass would otherwise be written to
the cache as a permanent, authoritative HOLD, silently corrupting every
future replay of that key.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Literal, Self

from wit.committee.contract import CommitteeDecision
from wit.committee.provider import DecisionProvider
from wit.desks.quant_analyst import QuantAnalystReport

Mode = Literal["strict", "record"]

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS decisions (
    instrument_id TEXT NOT NULL,
    bar_ts_ns INTEGER NOT NULL,
    model_key TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (instrument_id, bar_ts_ns, model_key, report_hash)
)
"""

_DECISION_FIELDS = {f.name for f in fields(CommitteeDecision)}


class CacheMissError(RuntimeError):
    """Raised by ``ReplayCommitteeProvider`` in ``strict`` mode on a cache miss."""


def _json_default(obj: Any) -> Any:
    """Coerces numpy-scalar-shaped values (``.item()``) that the desks'
    array math can end up in ``CommitteeDecision.detail``. Anything else
    still raises ``TypeError`` — better a loud failure at record time than a
    silently corrupted cache row."""
    item = getattr(obj, "item", None)
    if callable(item):
        return item()
    raise TypeError(f"object of type {type(obj).__name__} is not JSON serializable")


def _report_hash(report: QuantAnalystReport) -> str:
    payload = f"{report.timeframe}\n{report.as_prompt_block()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReplayCommitteeProvider:
    """Implements ``DecisionProvider`` over a SQLite-backed decision cache.

    Thread-safe: opened with ``check_same_thread=False`` and a lock around
    every read/write, since ``decide()`` is invoked via NautilusTrader's
    ``run_in_executor`` — a multi-worker thread pool, not the thread that
    constructed this provider (Phase N3 audit, finding F2).
    """

    def __init__(
        self,
        cache_path: str | Path,
        mode: Mode = "strict",
        live: DecisionProvider | None = None,
        model_key: str = "",
    ):
        if mode == "record" and live is None:
            raise ValueError("record mode needs a live provider to call through to")
        self.mode = mode
        self.model_key = model_key
        self._live = live
        self._lock = threading.Lock()
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(cache_path), check_same_thread=False)
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def decide(
        self, report: QuantAnalystReport, *, instrument_id: str = "", bar_ts_ns: int = 0
    ) -> CommitteeDecision:
        key = (instrument_id, bar_ts_ns, self.model_key, _report_hash(report))
        cached = self._get(key)
        if cached is not None:
            return cached

        if self.mode == "strict":
            raise CacheMissError(
                f"no cached decision for instrument_id={instrument_id!r} "
                f"bar_ts_ns={bar_ts_ns} model_key={self.model_key!r} "
                f"(symbol={report.symbol}) — record it first with a "
                f"`record`-mode ReplayCommitteeProvider using the same model_key"
            )

        assert self._live is not None  # enforced at __init__
        decision = self._live.decide(report, instrument_id=instrument_id, bar_ts_ns=bar_ts_ns)
        if decision.error is None:
            self._put(key, decision)
        return decision

    # -- cache I/O ---------------------------------------------------------
    def _get(self, key: tuple[str, int, str, str]) -> CommitteeDecision | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT decision_json FROM decisions "
                "WHERE instrument_id = ? AND bar_ts_ns = ? AND model_key = ? AND report_hash = ?",
                key,
            ).fetchone()
        if row is None:
            return None
        raw = {k: v for k, v in json.loads(row[0]).items() if k in _DECISION_FIELDS}
        return CommitteeDecision(**raw)

    def _put(self, key: tuple[str, int, str, str], decision: CommitteeDecision) -> None:
        instrument_id, bar_ts_ns, model_key, report_hash = key
        payload = json.dumps(asdict(decision), default=_json_default)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO decisions "
                "(instrument_id, bar_ts_ns, model_key, report_hash, decision_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (instrument_id, bar_ts_ns, model_key, report_hash, payload),
            )
            self._conn.commit()
