"""
Alerts and health-file writer.

Two outputs:
  1. data/health.json — overwritten every tick with full HealthState snapshot.
     External tools / dashboards / monitors can read this.
  2. Discord/Telegram webhook — fires on critical state changes:
       * No detection for >5 min
       * GQL hash rotated
       * Account died (cookie expired)
       * XTracker divergence
       * Process restarted

Configuration via env vars:
  POLYBOT_DISCORD_WEBHOOK   — full webhook URL
  POLYBOT_TELEGRAM_BOT_TOKEN + POLYBOT_TELEGRAM_CHAT_ID
  POLYBOT_HEALTH_FILE       — path to health.json (default data/health.json)
  POLYBOT_ALERT_NO_SIGNAL_S — seconds of no detection before alerting (default 600)
  POLYBOT_DAILY_SUMMARY     — "1" to enable daily summary at UTC midnight
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from signals.health_state import HealthState, Alert


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if v else default


class AlertSink:
    """
    Owns the Discord/Telegram delivery channels.
    Never blocks the caller — sends are queued and dispatched off-thread.
    """

    def __init__(self, log: Optional[logging.Logger] = None):
        self.log = log or logging.getLogger("alerts")
        self.discord_url = _env("POLYBOT_DISCORD_WEBHOOK")
        self.tg_token = _env("POLYBOT_TELEGRAM_BOT_TOKEN")
        self.tg_chat = _env("POLYBOT_TELEGRAM_CHAT_ID")
        self._lock = threading.Lock()
        self._queue: list[dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.discord_url or (self.tg_token and self.tg_chat))

    def send(self, level: str, title: str, body: str = "") -> None:
        """Non-blocking enqueue. Worker dispatches."""
        if not self.enabled:
            return
        with self._lock:
            self._queue.append({"level": level, "title": title, "body": body, "ts": time.time()})

    def _deliver_discord(self, msg: dict) -> bool:
        if not self.discord_url:
            return False
        color = {"info": 0x3498db, "warn": 0xf39c12, "critical": 0xe74c3c}.get(
            msg["level"], 0x95a5a6
        )
        payload = {
            "embeds": [{
                "title": f"[{msg['level'].upper()}] {msg['title']}",
                "description": msg["body"][:4000],
                "color": color,
                "timestamp": datetime.fromtimestamp(msg["ts"], tz=timezone.utc).isoformat(),
            }]
        }
        try:
            r = self._session.post(self.discord_url, json=payload, timeout=10.0)
            return r.status_code in (200, 204)
        except Exception as e:
            self.log.warning("discord deliver failed: %s", e)
            return False

    def _deliver_telegram(self, msg: dict) -> bool:
        if not (self.tg_token and self.tg_chat):
            return False
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        emoji = {"info": "ℹ️", "warn": "⚠️", "critical": "🚨"}.get(msg["level"], "•")
        text = f"{emoji} *{msg['title']}*\n{msg['body']}"[:4000]
        try:
            r = self._session.post(
                url,
                data={"chat_id": self.tg_chat, "text": text, "parse_mode": "Markdown"},
                timeout=10.0,
            )
            return r.status_code == 200
        except Exception as e:
            self.log.warning("telegram deliver failed: %s", e)
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                pending = self._queue
                self._queue = []
            for msg in pending:
                self._deliver_discord(msg)
                self._deliver_telegram(msg)
            self._stop.wait(2.0)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alert_sink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


class HealthMonitor:
    """
    Reads HealthState every tick and:
      - Writes data/health.json
      - Fires alerts when threshold conditions are met:
          * no detection for > alert_no_signal_s
          * all accounts dead
          * critical alerts surfacing from HealthState (XTracker divergence,
            hash rotation, etc.) get delivered through AlertSink
      - Posts daily summary at UTC midnight (if enabled)
    """

    def __init__(
        self,
        health: HealthState,
        sink: AlertSink,
        health_file: str = "data/health.json",
        tick_s: float = 5.0,
        alert_no_signal_s: float = 600.0,
        daily_summary: bool = True,
        log: Optional[logging.Logger] = None,
    ):
        self.health = health
        self.sink = sink
        self.health_file = _env("POLYBOT_HEALTH_FILE", health_file)
        try:
            no_sig_env = _env("POLYBOT_ALERT_NO_SIGNAL_S")
            self.alert_no_signal_s = float(no_sig_env) if no_sig_env else alert_no_signal_s
        except Exception:
            self.alert_no_signal_s = alert_no_signal_s
        self.daily_summary = daily_summary if _env("POLYBOT_DAILY_SUMMARY") != "0" else False
        self.tick_s = tick_s
        self.log = log or logging.getLogger("health_monitor")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_daily_summary_date: Optional[str] = None
        # ensure directory exists
        try:
            os.makedirs(os.path.dirname(self.health_file) or ".", exist_ok=True)
        except Exception:
            pass

    def _emit_pending_alerts(self) -> None:
        """Look at HealthState.recent_alerts; forward any un-webhooked ones."""
        for a in self.health.recent_alerts(limit=20):
            if a.fired_webhook:
                continue
            self.sink.send(level=a.level, title=a.code, body=a.message)
            self.health.mark_alert_webhooked(a)

    def _check_no_signal(self) -> None:
        age = self.health.last_detection_age()
        # only meaningful once we've actually detected something or been up a while
        snap = self.health.snapshot()
        if snap["uptime_s"] < 120:
            return
        if not (isinstance(age, (int, float)) and math.isfinite(age)) and snap["uptime_s"] < self.alert_no_signal_s:
            return
        if age > self.alert_no_signal_s:
            age_label = (
                f"{int(age)}s"
                if isinstance(age, (int, float)) and math.isfinite(age)
                else f"startup uptime {int(snap['uptime_s'])}s"
            )
            a = self.health.alert(
                level="critical",
                code="no_signal",
                message=f"No tweet detected by ANY source for {age_label} "
                        f"(threshold={int(self.alert_no_signal_s)}s)",
                dedup_s=self.alert_no_signal_s,
            )
            if a:
                self.sink.send(level=a.level, title=a.code, body=a.message)
                self.health.mark_alert_webhooked(a)

    def _check_no_live_accounts(self, snap: dict) -> None:
        # We don't have direct access to rotator from health snapshot, but we
        # CAN see if any "gql:*" source has been heartbeating
        sources = snap.get("sources", {})
        gql_alive = any(
            v.get("alive") for k, v in sources.items()
            if k.startswith("gql") or k.startswith("rest")
        )
        if not gql_alive and snap["uptime_s"] > 300:
            a = self.health.alert(
                level="critical",
                code="no_live_gql",
                message="No live GQL/REST sources — all accounts likely dead",
                dedup_s=900.0,
            )
            if a:
                self.sink.send(level=a.level, title=a.code, body=a.message)
                self.health.mark_alert_webhooked(a)

    def _maybe_daily_summary(self) -> None:
        if not self.daily_summary:
            return
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        # fire at first tick after midnight UTC
        if now.hour == 0 and self._last_daily_summary_date != today:
            self._last_daily_summary_date = today
            snap = self.health.snapshot()
            body_lines = [
                f"uptime: {snap['uptime_s']/3600:.1f}h",
                f"tweets detected (session): {snap['tweets_session']}",
                f"tweets detected (24h):    {snap['tweets_24h']}",
                f"lifetime_count: {snap.get('lifetime_count')} via {snap.get('lifetime_count_source')}",
                "sources:",
            ]
            for name, s in snap.get("sources", {}).items():
                body_lines.append(
                    f"  {name}: alive={s.get('alive')} "
                    f"last_ok={s.get('last_success_age_s')}s "
                    f"401x={s.get('consecutive_401')}"
                )
            self.sink.send(
                level="info",
                title=f"polybot daily summary {today}",
                body="\n".join(body_lines),
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.health.write_to_file(self.health_file)
                snap = self.health.snapshot()
                self._emit_pending_alerts()
                self._check_no_signal()
                self._check_no_live_accounts(snap)
                self._maybe_daily_summary()
            except Exception:
                self.log.exception("health monitor tick crashed")
            self._stop.wait(self.tick_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="health_monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
