"""Alerting — console always, Telegram when configured.

Telegram is optional and best-effort: an alerting outage must never propagate
into the trading loop, so every send failure is swallowed and reported to the
console instead.

Ported verbatim from ``Wit-Hedge-fund/engine/alerts.py`` (Phase N7).
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Alerter:
    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = True

    @classmethod
    def from_env(cls) -> Alerter:
        return cls(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        )

    @property
    def telegram_ready(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str) -> bool:
        """Print, and mirror to Telegram if configured. Returns True if sent."""
        print(text)
        if not (self.enabled and self.telegram_ready):
            return False
        payload = json.dumps({
            "chat_id": self.chat_id, "text": text, "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        # http.client.HTTPException (e.g. IncompleteRead, BadStatusLine)
        # derives from Exception, not OSError, and would otherwise escape
        # this catch (Phase N7 audit finding L3) - every caller already
        # wraps send() in its own except Exception, so this was never a
        # live crash risk, but the docstring's "every send failure is
        # swallowed" should actually be true here, not just downstream.
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            print(f"[alert] Telegram send failed (trading unaffected): {e}")
            return False
