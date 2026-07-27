"""The live committee: bull/bear researchers -> Portfolio Manager, over the
Anthropic API. Near-verbatim port of ``Wit-Hedge-fund/engine/agents_bridge.py``'s
``Committee``/``_RateLimiter`` (Phase N3) — see ``wit/committee/provider.py``
for why this stays synchronous rather than becoming an async rewrite.

Defaults to direct Anthropic (``LLMConfig.base_url`` empty -> the SDK's own
default endpoint) rather than a free-tier gateway: the MT5 build's own notes
record its gateway (NaraRouter) silently substituting a different model than
requested, which is a real-money risk for a system that places live orders.
``served_model`` is still logged either way, since it's the only way to
detect substitution if a gateway is used later.
"""
from __future__ import annotations

import time
from typing import Any

from wit.committee.contract import (
    DEBATE_DISTINCTIVENESS_FLOOR,
    Action,
    CommitteeDecision,
    distinctiveness,
)
from wit.committee.prompts import (
    _DECISION_TOOL,
    _DREAM_SYSTEM,
    _LESSONS_TOOL,
    _PM_SYSTEM,
    _RESEARCHER_SYSTEM,
)
from wit.config import CONFIG, LLMConfig
from wit.desks.quant_analyst import QuantAnalystReport


class _RateLimiter:
    """Paces calls to at most ``rpm`` per rolling minute via fixed spacing.

    Shared across all three calls (bull, bear, PM) since they draw from the
    same provider-side budget. Proactive pacing keeps 429-driven abstains
    unreachable in the common case rather than hoping SDK retries cover for
    it — a rate-limit abstain looks identical in the journal to a genuine
    no-edge decision.
    """

    def __init__(self, rpm: int):
        self._min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last_call: float | None = None

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        if self._last_call is not None:
            remaining = self._min_interval - (time.monotonic() - self._last_call)
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()


class LiveCommitteeProvider:
    """Runs the debate and returns the PM's verdict. Implements ``DecisionProvider``."""

    def __init__(self, llm: LLMConfig | None = None, timeframe: str | None = None):
        self.llm = llm or CONFIG.llm
        self.timeframe = timeframe or CONFIG.timeframe
        if not self.llm.api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set — cannot run the committee")
        # Fail fast on a half-configured .env: an empty model name makes every
        # symbol quietly abstain to HOLD, indistinguishable from a real no-edge
        # decision. One clear error at construction beats silent no-ops.
        missing = [name for name, val in
                   (("WIT_DEEP_MODEL", self.llm.deep_model),
                    ("WIT_QUICK_MODEL", self.llm.quick_model)) if not val]
        if missing:
            raise ValueError(f"{' and '.join(missing)} not set in .env")
        import anthropic

        client_kwargs: dict[str, Any] = {
            "api_key": self.llm.api_key,
            "max_retries": 3,   # safety net on top of pacing — not so high it
            "timeout": 90.0,    # compounds into multi-minute stalls on its own
        }
        if self.llm.base_url:
            client_kwargs["base_url"] = self.llm.base_url
        self._client = anthropic.Anthropic(**client_kwargs)
        self._limiter = _RateLimiter(self.llm.rpm_limit)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def build_context(report: QuantAnalystReport) -> str:
        return report.as_prompt_block()

    def _researcher(self, side: str, direction: str, symbol: str, context: str) -> str:
        self._limiter.wait()
        msg = self._client.messages.create(
            model=self.llm.quick_model,
            max_tokens=600,
            system=_RESEARCHER_SYSTEM.format(
                side=side, direction=direction, symbol=symbol, timeframe=self.timeframe
            ),
            messages=[{"role": "user", "content": context}],
        )
        if not msg.content:
            raise ValueError(
                f"{self.llm.quick_model} returned empty content "
                f"(stop_reason={msg.stop_reason!r})"
            )
        return "".join(b.text for b in msg.content if b.type == "text").strip()

    def _portfolio_manager(
        self, symbol: str, context: str, bull: str, bear: str
    ) -> CommitteeDecision:
        user = (
            f"{context}\n"
            f"BULL RESEARCHER\n{bull}\n\n"
            f"BEAR RESEARCHER\n{bear}\n\n"
            f"Deliver your verdict on {symbol}."
        )
        self._limiter.wait()
        msg = self._client.messages.create(
            model=self.llm.deep_model,
            max_tokens=1500,
            system=_PM_SYSTEM.format(symbol=symbol, timeframe=self.timeframe),
            tools=[_DECISION_TOOL],
            tool_choice={"type": "tool", "name": "submit_decision"},
            messages=[{"role": "user", "content": user}],
        )
        if not msg.content:
            return CommitteeDecision.abstain(
                symbol, f"PM returned empty content (stop_reason={msg.stop_reason!r})"
            )
        block = next((b for b in msg.content if b.type == "tool_use"), None)
        if block is None:
            return CommitteeDecision.abstain(symbol, "PM returned no tool call")

        d = block.input
        action: Action = d["action"]
        # A HOLD must never carry size, whatever the model reported.
        conviction = 0.0 if action == "HOLD" else float(d["conviction"])
        dist = distinctiveness(bull, bear)
        return CommitteeDecision(
            symbol=symbol,
            action=action,
            conviction=max(0.0, min(1.0, conviction)),
            risk_rating=d["risk_rating"],
            rationale=d["rationale"],
            key_risk=d["key_risk"],
            stop_atr_mult=float(d["stop_atr_mult"]),
            reward_risk=float(d["reward_risk"]),
            bull_case=bull,
            bear_case=bear,
            model=self.llm.deep_model,
            served_model=(getattr(msg, "model", "") or "").strip() or self.llm.deep_model,
            detail={"usage": {"input": msg.usage.input_tokens,
                              "output": msg.usage.output_tokens},
                    "debate_distinctiveness": dist,
                    "debate_degenerate": dist < DEBATE_DISTINCTIVENESS_FLOOR},
        )

    # -- DecisionProvider entry point --------------------------------------
    def decide(
        self, report: QuantAnalystReport, *, instrument_id: str = "", bar_ts_ns: int = 0
    ) -> CommitteeDecision:
        """Run bull/bear research then the PM verdict. Never raises.
        ``instrument_id``/``bar_ts_ns`` are accepted for ``DecisionProvider``
        signature compatibility and unused here — see that protocol's docstring."""
        symbol = report.symbol
        try:
            context = self.build_context(report)
            bull = self._researcher("Bull", "long", symbol, context)
            bear = self._researcher("Bear", "short", symbol, context)
            return self._portfolio_manager(symbol, context, bull, bear)
        except Exception as e:  # noqa: BLE001 - an LLM outage must not trade
            return CommitteeDecision.abstain(symbol, f"{type(e).__name__}: {e}")

    # -- weekly self-review (Phase N7) -------------------------------------
    def dream(
        self, qualifying: dict[str, dict[str, dict]], scores: list[dict[str, Any]],
        window_days: int, min_bucket_trades: int,
    ) -> list[dict[str, Any]]:
        """One deep-model call over sample-floor-filtered performance buckets.

        Returns raw ``{lesson, dimension, key, confidence}`` dicts — validating
        and attaching the real basis numbers is ``wit.ops.dream``'s job, not
        this one. Never raises: any failure yields no lessons rather than
        blocking the weekly cycle.
        """
        import json

        try:
            self._limiter.wait()
            msg = self._client.messages.create(
                model=self.llm.deep_model,
                max_tokens=800,
                system=_DREAM_SYSTEM.format(
                    window_days=window_days, min_bucket_trades=min_bucket_trades
                ),
                tools=[_LESSONS_TOOL],
                tool_choice={"type": "tool", "name": "submit_lessons"},
                messages=[{"role": "user", "content": json.dumps(
                    {"buckets": qualifying, "previous_lesson_scorecards": scores}, indent=2,
                )}],
            )
            if not msg.content:
                return []
            block = next((b for b in msg.content if b.type == "tool_use"), None)
            if block is None:
                return []
            return block.input.get("lessons", [])
        except Exception:  # noqa: BLE001 - a dream-cycle outage must not disrupt trading
            return []
