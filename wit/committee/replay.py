"""``ReplayCommitteeProvider`` — a SQLite decision cache keyed by
``(instrument_id, bar_ts_ns, sha256(prompt_block))`` (build plan §1.2 / §3
Phase N3).

This is new code, not a port: the MT5 build has no backtester, so nothing
like it existed to port. The cache is what makes a full-fidelity backtest of
an LLM-mediated strategy affordable to run more than once — pay the API bill
once per ``(symbol, bar)`` in ``record`` mode, then replay those exact
decisions for free while sweeping risk/sizing parameters in ``strict`` mode.

Two modes:

- ``record``: cache hit returns the cached decision; cache miss calls the
  wrapped live provider once and writes the result through before returning
  it. Used to populate the cache from real committee runs.
- ``strict``: cache hit returns the cached decision; cache miss **raises**
  rather than abstaining. A missing entry during a `strict` backtest means
  the cache is incomplete for the run being replayed — silently substituting
  a HOLD would quietly distort the backtest's P&L instead of surfacing the
  gap, which is worse than stopping. This is a deliberate exception to
  ``DecisionProvider.decide``'s general "never raises" contract, which exists
  to keep a live LLM outage from blocking trading; a replay cache miss is an
  operator/setup error, not a live outage, and deserves to fail loudly.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Literal, Self

from wit.committee.contract import CommitteeDecision
from wit.committee.provider import DecisionProvider
from wit.desks.quant_analyst import QuantAnalystReport

Mode = Literal["strict", "record"]

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS decisions (
    instrument_id TEXT NOT NULL,
    bar_ts_ns INTEGER NOT NULL,
    report_hash TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (instrument_id, bar_ts_ns, report_hash)
)
"""


class CacheMissError(RuntimeError):
    """Raised by ``ReplayCommitteeProvider`` in ``strict`` mode on a cache miss."""


def _report_hash(report: QuantAnalystReport) -> str:
    return hashlib.sha256(report.as_prompt_block().encode("utf-8")).hexdigest()


class ReplayCommitteeProvider:
    """Implements ``DecisionProvider`` over a SQLite-backed decision cache."""

    def __init__(
        self,
        cache_path: str | Path,
        mode: Mode = "strict",
        live: DecisionProvider | None = None,
    ):
        if mode == "record" and live is None:
            raise ValueError("record mode needs a live provider to call through to")
        self.mode = mode
        self._live = live
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(cache_path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def decide(
        self, report: QuantAnalystReport, *, instrument_id: str = "", bar_ts_ns: int = 0
    ) -> CommitteeDecision:
        key = (instrument_id, bar_ts_ns, _report_hash(report))
        cached = self._get(key)
        if cached is not None:
            return cached

        if self.mode == "strict":
            raise CacheMissError(
                f"no cached decision for instrument_id={instrument_id!r} "
                f"bar_ts_ns={bar_ts_ns} (symbol={report.symbol}) — record it first "
                f"with a `record`-mode ReplayCommitteeProvider"
            )

        assert self._live is not None  # enforced at __init__
        decision = self._live.decide(report, instrument_id=instrument_id, bar_ts_ns=bar_ts_ns)
        self._put(key, decision)
        return decision

    # -- cache I/O ---------------------------------------------------------
    def _get(self, key: tuple[str, int, str]) -> CommitteeDecision | None:
        row = self._conn.execute(
            "SELECT decision_json FROM decisions "
            "WHERE instrument_id = ? AND bar_ts_ns = ? AND report_hash = ?",
            key,
        ).fetchone()
        if row is None:
            return None
        return CommitteeDecision(**json.loads(row[0]))

    def _put(self, key: tuple[str, int, str], decision: CommitteeDecision) -> None:
        instrument_id, bar_ts_ns, report_hash = key
        self._conn.execute(
            "INSERT OR REPLACE INTO decisions "
            "(instrument_id, bar_ts_ns, report_hash, decision_json) VALUES (?, ?, ?, ?)",
            (instrument_id, bar_ts_ns, report_hash, json.dumps(asdict(decision))),
        )
        self._conn.commit()
