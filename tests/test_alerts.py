"""``wit/ops/alerts.py`` — console always, Telegram best-effort. A Telegram
outage must never propagate into the trading loop, so every send failure is
swallowed."""
from __future__ import annotations

import urllib.error

from wit.ops.alerts import Alerter


def test_from_env_reads_telegram_vars(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " tok ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " 123 ")
    alerter = Alerter.from_env()
    assert alerter.bot_token == "tok"
    assert alerter.chat_id == "123"
    assert alerter.telegram_ready


def test_from_env_not_ready_without_both_vars(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert not Alerter.from_env().telegram_ready


def test_send_without_telegram_prints_and_returns_false(capsys):
    alerter = Alerter(bot_token="", chat_id="")
    assert alerter.send("hello") is False
    assert "hello" in capsys.readouterr().out


def test_send_disabled_never_calls_telegram_even_when_configured(monkeypatch, capsys):
    called = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: called.append(1))
    alerter = Alerter(bot_token="t", chat_id="c", enabled=False)
    assert alerter.send("hello") is False
    assert called == []


def test_send_with_telegram_configured_posts_and_returns_true(monkeypatch):
    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    captured = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    alerter = Alerter(bot_token="tok", chat_id="123")
    assert alerter.send("hello world") is True
    assert "tok" in captured["url"]
    assert b"hello world" in captured["data"]


def test_send_swallows_a_network_failure_and_reports_false(monkeypatch, capsys):
    def raise_it(req, timeout=10):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", raise_it)
    alerter = Alerter(bot_token="tok", chat_id="123")
    assert alerter.send("hello") is False
    out = capsys.readouterr().out
    assert "hello" in out
    assert "Telegram send failed" in out


def test_send_swallows_a_non_200_status(monkeypatch):
    class _Resp:
        status = 500
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=10: _Resp())
    alerter = Alerter(bot_token="tok", chat_id="123")
    assert alerter.send("hello") is False
