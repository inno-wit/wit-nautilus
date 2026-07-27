"""Phase N1 gate: the package imports and the CLI's doctor command runs end-to-end.

Real desk/committee/risk/nautilus tests land in their owning phases (N2-N7).
"""
from wit.cli import build_parser, cmd_doctor
from wit.config import CONFIG, IBConfig, LLMConfig, SafetyConfig


def test_config_loads_with_typed_defaults():
    assert isinstance(CONFIG.llm, LLMConfig)
    assert isinstance(CONFIG.ib, IBConfig)
    assert isinstance(CONFIG.safety, SafetyConfig)
    assert CONFIG.ib.port == 4002  # paper by default until .env overrides it
    assert CONFIG.safety.paper_only is True


def test_cli_doctor_runs_without_raising():
    parser = build_parser()
    args = parser.parse_args(["doctor"])
    # No assertion on exit code — a fresh checkout with no .env is expected to report
    # missing keys (exit 1), not raise. That's the behavior this test locks in.
    result = cmd_doctor(args)
    assert result in (0, 1)
