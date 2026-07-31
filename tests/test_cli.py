"""``wit/cli.py`` — the Phase N7 command surface (``halt``/``resume``/
``status``/``review``/``dream``), plus ``doctor``'s new LLM round-trip.
``paper``/``live`` aren't exercised here beyond argument parsing - they boot
a real ``TradingNode`` and belong to Phase N9's attended gate.
"""
from __future__ import annotations

from wit import cli


def _config(tmp_path, monkeypatch, **overrides):
    from wit.config import Config, SafetyConfig

    cfg = Config(safety=SafetyConfig(kill_switch_file=str(tmp_path / "KILL")), **overrides)
    monkeypatch.setattr(cli, "CONFIG", cfg)
    return cfg


# ── halt / resume / status ──────────────────────────────────────────────

def test_halt_writes_the_kill_switch_file(tmp_path, monkeypatch, capsys):
    cfg = _config(tmp_path, monkeypatch)
    rc = cli.cmd_halt(cli.build_parser().parse_args(["halt", "--reason", "testing"]))
    assert rc == 0
    assert _path_exists(cfg.safety.kill_switch_file)
    assert "testing" in _path_read(cfg.safety.kill_switch_file)


def test_resume_removes_the_kill_switch_file(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    cli.cmd_halt(cli.build_parser().parse_args(["halt"]))
    rc = cli.cmd_resume(cli.build_parser().parse_args(["resume"]))
    assert rc == 0
    assert not _path_exists(cfg.safety.kill_switch_file)


def test_resume_when_not_engaged_is_a_no_op(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    rc = cli.cmd_resume(cli.build_parser().parse_args(["resume"]))
    assert rc == 0
    assert "not engaged" in capsys.readouterr().out


def test_status_reports_kill_switch_state(tmp_path, monkeypatch, capsys):
    _config(tmp_path, monkeypatch)
    cli.cmd_halt(cli.build_parser().parse_args(["halt", "--reason", "drawdown"]))
    out = _run_status()
    assert "ENGAGED" in out
    assert "drawdown" in out


def test_status_reports_clear_when_not_engaged(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    assert "clear" in _run_status()


def _run_status():
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_status(cli.build_parser().parse_args(["status"]))
    return buf.getvalue()


def _path_exists(p: str) -> bool:
    from pathlib import Path
    return Path(p).exists()


def _path_read(p: str) -> str:
    from pathlib import Path
    return Path(p).read_text(encoding="utf-8")


# ── review ───────────────────────────────────────────────────────────────

def test_review_prints_reflection_summary(tmp_path, monkeypatch, capsys):
    from wit.config import Config, SafetyConfig
    journal_path = tmp_path / "journal.jsonl"
    cfg = Config(safety=SafetyConfig(kill_switch_file=str(tmp_path / "KILL")),
                journal_path=str(journal_path))
    monkeypatch.setattr(cli, "CONFIG", cfg)

    from wit.ops.journal import Journal
    Journal(str(journal_path)).log_event("test_event", "hello")

    rc = cli.cmd_review(cli.build_parser().parse_args(["review", "--days", "7"]))
    assert rc == 0
    assert "Reflection" in capsys.readouterr().out


def test_review_scores_a_completed_round_trip_from_the_journal_alone(tmp_path, monkeypatch, capsys):
    """Phase N7 audit finding C1: review no longer needs an external P&L
    source (the removed --pnl-json) - Reflection.review() reads realized
    P&L straight from the journal's own position_closed events."""
    from dataclasses import dataclass as dc

    from wit.config import Config, SafetyConfig
    journal_path = tmp_path / "journal.jsonl"
    cfg = Config(safety=SafetyConfig(kill_switch_file=str(tmp_path / "KILL")),
                journal_path=str(journal_path))
    monkeypatch.setattr(cli, "CONFIG", cfg)

    @dc
    class _D:
        conviction: float = 0.7
        def to_dict(self): return {"conviction": self.conviction}

    @dc
    class _P:
        action: str = "BUY"
        def to_dict(self): return {"action": self.action}

    @dc
    class _R:
        def to_dict(self): return {"markov": {"regime": "Bull"}, "garch": {"vol_regime": "calm"}}

    from wit.ops.journal import Journal
    journal = Journal(str(journal_path))
    journal.log_decision("NVDA", _D(), _P(), _R(),
                         order={"ok": True, "client_order_id": "O-1"}, client_order_id="O-1")
    journal.log_event("position_closed", "realized_pnl=88.0", symbol="NVDA",
                      position_id="NVDA.SIM-Strategy-000", opening_order_id="O-1",
                      realized_pnl=88.0)

    cli.cmd_review(cli.build_parser().parse_args(["review"]))
    out = capsys.readouterr().out
    assert "88.00" in out or "+88.00" in out


# ── doctor ───────────────────────────────────────────────────────────────
#
# Every test here passes empty AlpacaConfig/PolygonConfig explicitly (not just
# relying on Config()'s defaults, which read real .env-sourced keys via
# AlpacaConfig/PolygonConfig's own field defaults) - doctor now makes real
# read-only network calls when keys ARE present (this module's docstring),
# and a unit test must never depend on live credentials or network access.

def test_doctor_reports_missing_env_as_problems(tmp_path, monkeypatch, capsys):
    from wit.config import AlpacaConfig, Config, LLMConfig, PolygonConfig, SafetyConfig
    cfg = Config(
        safety=SafetyConfig(kill_switch_file=str(tmp_path / "KILL"), paper_only=True),
        llm=LLMConfig(api_key="", deep_model="", quick_model=""),
        alpaca=AlpacaConfig(api_key="", secret_key=""),
        polygon=PolygonConfig(api_key=""),
    )
    monkeypatch.setattr(cli, "CONFIG", cfg)

    rc = cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
    out = capsys.readouterr().out
    assert rc == 1
    assert "ANTHROPIC_API_KEY" in out
    assert "ALPACA_API_KEY/ALPACA_SECRET_KEY" in out
    assert "Alpaca    : SKIPPED" in out
    assert "Polygon   : SKIPPED" in out


def test_doctor_flags_paper_only_false(tmp_path, monkeypatch, capsys):
    from wit.config import AlpacaConfig, Config, LLMConfig, PolygonConfig, SafetyConfig
    cfg = Config(
        safety=SafetyConfig(kill_switch_file=str(tmp_path / "KILL"), paper_only=False),
        llm=LLMConfig(api_key="", deep_model="", quick_model=""),
        alpaca=AlpacaConfig(api_key="PKTEST", secret_key="test", paper=True),
        polygon=PolygonConfig(api_key=""),
    )
    monkeypatch.setattr(cli, "CONFIG", cfg)
    rc = cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
    assert rc == 1
    assert "WIT_PAPER_ONLY is false" in capsys.readouterr().out


def test_doctor_flags_alpaca_paper_false(tmp_path, monkeypatch, capsys):
    from wit.config import AlpacaConfig, Config, LLMConfig, PolygonConfig, SafetyConfig
    cfg = Config(
        safety=SafetyConfig(kill_switch_file=str(tmp_path / "KILL"), paper_only=True),
        llm=LLMConfig(api_key="", deep_model="", quick_model=""),
        alpaca=AlpacaConfig(api_key="", secret_key="", paper=False),
        polygon=PolygonConfig(api_key=""),
    )
    monkeypatch.setattr(cli, "CONFIG", cfg)
    rc = cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
    assert rc == 1
    assert "ALPACA_PAPER is false" in capsys.readouterr().out


def test_doctor_reports_alpaca_and_polygon_as_skipped_not_attempted_without_keys(
    tmp_path, monkeypatch, capsys,
):
    """Regression guard for this module's own documented scope limit -
    doctor reports Alpaca/Polygon connectivity as skipped (not attempted, no
    network call) when no key is configured."""
    from wit.config import AlpacaConfig, Config, LLMConfig, PolygonConfig, SafetyConfig
    cfg = Config(
        safety=SafetyConfig(kill_switch_file=str(tmp_path / "KILL"), paper_only=True),
        llm=LLMConfig(api_key="", deep_model="", quick_model=""),
        alpaca=AlpacaConfig(api_key="", secret_key=""),
        polygon=PolygonConfig(api_key=""),
    )
    monkeypatch.setattr(cli, "CONFIG", cfg)
    cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
    out = capsys.readouterr().out
    assert "Alpaca    : SKIPPED" in out
    assert "Polygon   : SKIPPED" in out


# ── live requires --i-know ──────────────────────────────────────────────

def test_live_refuses_without_i_know(capsys):
    rc = cli.cmd_live(cli.build_parser().parse_args(["live"]))
    assert rc == 1
    assert "--i-know" in capsys.readouterr().out


def test_live_parses_i_know_flag():
    args = cli.build_parser().parse_args(["live", "--i-know"])
    assert args.i_know is True


def test_paper_and_live_are_separate_subcommands():
    parser = cli.build_parser()
    assert parser.parse_args(["paper"]).func is cli.cmd_paper
    assert parser.parse_args(["live"]).func is cli.cmd_live
