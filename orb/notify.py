from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("orb.notify")


class Notifier:
    """Console/log alerts plus an optional webhook (Discord-compatible JSON).

    The webhook URL is read from config `alerts.webhook_url` or the
    ORB_WEBHOOK_URL environment variable (preferred, keeps secrets out of git).
    """

    def __init__(self, cfg: dict):
        alerts = cfg.get("alerts", {})
        self.enabled = bool(alerts.get("enabled", False))
        self.url = os.environ.get("ORB_WEBHOOK_URL") or alerts.get("webhook_url", "")
        self.kind = alerts.get("kind", "discord")

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        log.info("ALERT: %s", message)
        if not self.url:
            return
        if self.kind == "discord":
            payload = {"content": message}
        elif self.kind == "slack":
            payload = {"text": message}
        else:
            payload = {"text": message}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the bot
            log.warning("webhook alert failed: %s", exc)
