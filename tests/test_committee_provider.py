"""Verification for ``build_committee_provider`` — the switch that decides
which committee actually trades. Had zero direct coverage before this file
(opus-audit round 1 finding): everything downstream (``node_live.py``,
``cli.py``'s ``cmd_dream``) trusts this function to route correctly."""
from __future__ import annotations

import pytest

from wit.committee.live import LiveCommitteeProvider
from wit.committee.provider import build_committee_provider
from wit.committee.rules import RulePolicyProvider
from wit.config import Config, LLMConfig


def test_rules_mode_returns_rule_policy_provider_with_no_llm_config_at_all():
    cfg = Config(committee_mode="rules")
    provider = build_committee_provider(cfg)
    assert isinstance(provider, RulePolicyProvider)


def test_llm_is_the_default_mode_and_returns_live_committee_provider():
    cfg = Config(llm=LLMConfig(api_key="k", deep_model="d", quick_model="q", nara_api_key="nk"))
    assert cfg.committee_mode == "llm"
    provider = build_committee_provider(cfg)
    assert isinstance(provider, LiveCommitteeProvider)


def test_unrecognized_committee_mode_raises_instead_of_silently_running_llm():
    """A typo'd WIT_COMMITTEE_MODE (e.g. "rule") must not silently fall
    through to the LLM committee an operator thought they'd turned off."""
    cfg = Config(committee_mode="rule")
    with pytest.raises(ValueError, match="WIT_COMMITTEE_MODE"):
        build_committee_provider(cfg)


def test_empty_committee_mode_also_raises():
    cfg = Config(committee_mode="")
    with pytest.raises(ValueError, match="WIT_COMMITTEE_MODE"):
        build_committee_provider(cfg)
