"""
Central health and alert state for the watch process.

All sources (GQL poller, REST poller, SSE pool, XTracker, Playwright)
report their status here. The watchdog reads heartbeats to decide whether
to restart a thread. The alerts module reads state changes to fire
webhooks. The dashboard reads aggregated views for display.

Threading model: every method that touches mutable state takes self._lock.
Public read methods return snapshots (no live references) so callers
never see a half-written struct.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# Single source status. Sources: "gql", "rest", "xtracker", "sse:<label>", "playwright"
@dataclass
class SourceStatus:
    name: str
    alive: bool = False
    last_heartbeat: float = 0.0     # unix ts of last "I'm running" beat
    last_success: float = 0.0       # unix ts of last successful response
    last_detection: float = 0.0     # unix ts of last tweet detection
    consecutive_errors: int = 0
    consecutive_401: int = 0        # cookie expiry indicator
    last_error: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Alert:
    ts: float
    level: str           # "info" | "warn" | "critical"
    code: str            # short machine-readable code, eg "no_signal_5min"
    message: str
    source: str = ""     # which source triggered
    fired_webhook: bool = False


class HealthState:
    """
    Thread-safe central state.

    Use:
      hs.heartbeat("gql")                  # called by GQL thread every poll
      hs.mark_success("gql", detection=True)
      hs.mark_error("gql", "timeout")
      hs.mark_401("gql:qojjox")            # increments consecutive_401
      hs.alert("warn", "rate_limit_low", "All accounts < 10 rem", source="gql")
      hs.snapshot()                        # dashboard read
      hs.heartbeat_age("gql")              # watchdog read
    """

    def __init__(self, alert_log_max: int = 100):
        self._lock = threading.Lock()
        self._sources: dict[str, SourceStatus] = {}
        self._alerts: deque[Alert] = deque(maxlen=alert_log_max)
        self._alert_dedup: dict[str, float] = {}  # code -> last_fired_ts
        self._started_at = time.time()
        self._last_global_detection = 0.0   # any source detected anything
        self._last_global_success = 0.0     # any source succeeded
        self._lifetime_count: Optional[int] = None        # current best estimate
        self._lifetime_count_source: str = ""             # who reported it
        self._lifetime_count_ts: float = 0.0
        self._tweet_count_session = 0
        self._tweet_count_24h = deque()  # ts of detections in last 24h
        self._stop_requested = False
        self._restart_count: dict[str, int] = {}  # thread name -> times restarted

    # ── source registry ──────────────────────────────────────────────────

    def register(self, source: str) -> None:
        with self._lock:
            if source not in self._sources:
                self._sources[source] = SourceStatus(name=source)

    def _src(self, source: str) -> SourceStatus:
        """Caller must hold lock."""
        if source not in self._sources:
            self._sources[source] = SourceStatus(name=source)
        return self._sources[source]

    # ── heartbeats and outcomes (called from worker threads) ─────────────

    def heartbeat(self, source: str) -> None:
        now = time.time()
        with self._lock:
            s = self._src(source)
            s.last_heartbeat = now
            s.alive = True

    def mark_success(
        self,
        source: str,
        detection: bool = False,
        lifetime_count: Optional[int] = None,
        extra: Optional[dict] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            s = self._src(source)
            s.last_heartbeat = now
            s.last_success = now
            s.alive = True
            s.consecutive_errors = 0
            s.consecutive_401 = 0
            s.last_error = ""
            if detection:
                s.last_detection = now
                self._last_global_detection = now
                self._tweet_count_session += 1
                self._tweet_count_24h.append(now)
                # trim 24h window
                cutoff = now - 86400.0
                while self._tweet_count_24h and self._tweet_count_24h[0] < cutoff:
                    self._tweet_count_24h.popleft()
            self._last_global_success = now
            if lifetime_count is not None:
                self._lifetime_count = lifetime_count
                self._lifetime_count_source = source
                self._lifetime_count_ts = now
            if extra:
                s.extra.update(extra)

    def mark_error(self, source: str, reason: str = "") -> None:
        with self._lock:
            s = self._src(source)
            s.consecutive_errors += 1
            s.last_error = reason[:200]
            s.last_heartbeat = time.time()

    def mark_401(self, source: str) -> None:
        """Cookie expiry indicator. After 3 consecutive 401s, source is flagged dead."""
        with self._lock:
            s = self._src(source)
            s.consecutive_401 += 1
            s.last_error = "401"
            s.last_heartbeat = time.time()

    def mark_dead(self, source: str, reason: str = "") -> None:
        with self._lock:
            s = self._src(source)
            s.alive = False
            if reason:
                s.last_error = reason[:200]

    def mark_restart(self, thread_name: str) -> None:
        with self._lock:
            self._restart_count[thread_name] = self._restart_count.get(thread_name, 0) + 1

    # ── alerts ───────────────────────────────────────────────────────────

    def alert(
        self,
        level: str,
        code: str,
        message: str,
        source: str = "",
        dedup_s: float = 300.0,
    ) -> Optional[Alert]:
        """
        Record an alert. Suppresses duplicate codes within dedup_s seconds.
        Returns the Alert if it was recorded (and should be webhooked), else None.
        """
        now = time.time()
        with self._lock:
            last = self._alert_dedup.get(code, 0.0)
            if now - last < dedup_s:
                return None
            self._alert_dedup[code] = now
            a = Alert(ts=now, level=level, code=code, message=message, source=source)
            self._alerts.append(a)
            return a

    def mark_alert_webhooked(self, alert: Alert) -> None:
        with self._lock:
            alert.fired_webhook = True

    def recent_alerts(self, limit: int = 10) -> list[Alert]:
        with self._lock:
            return list(self._alerts)[-limit:]

    # ── reads ────────────────────────────────────────────────────────────

    def heartbeat_age(self, source: str) -> float:
        """Seconds since last heartbeat (inf if never seen)."""
        now = time.time()
        with self._lock:
            s = self._sources.get(source)
            if s is None or s.last_heartbeat == 0.0:
                return float("inf")
            return now - s.last_heartbeat

    def last_detection_age(self) -> float:
        """Seconds since ANY source last detected a tweet."""
        now = time.time()
        with self._lock:
            if self._last_global_detection == 0.0:
                return float("inf")
            return now - self._last_global_detection

    def last_success_age(self) -> float:
        now = time.time()
        with self._lock:
            if self._last_global_success == 0.0:
                return float("inf")
            return now - self._last_global_success

    def lifetime_count(self) -> tuple[Optional[int], str, float]:
        with self._lock:
            return self._lifetime_count, self._lifetime_count_source, self._lifetime_count_ts

    def update_lifetime_count(self, count: int, source: str) -> None:
        now = time.time()
        with self._lock:
            self._lifetime_count = count
            self._lifetime_count_source = source
            self._lifetime_count_ts = now

    def snapshot(self) -> dict:
        """Full snapshot for dashboard / health.json writer."""
        now = time.time()
        with self._lock:
            sources = {}
            for name, s in self._sources.items():
                sources[name] = {
                    "alive": s.alive,
                    "heartbeat_age_s": round(now - s.last_heartbeat, 1) if s.last_heartbeat else None,
                    "last_success_age_s": round(now - s.last_success, 1) if s.last_success else None,
                    "last_detection_age_s": round(now - s.last_detection, 1) if s.last_detection else None,
                    "consecutive_errors": s.consecutive_errors,
                    "consecutive_401": s.consecutive_401,
                    "last_error": s.last_error,
                    "extra": dict(s.extra),
                }
            return {
                "started_at": self._started_at,
                "uptime_s": round(now - self._started_at, 1),
                "now": now,
                "last_global_detection_age_s": (
                    round(now - self._last_global_detection, 1)
                    if self._last_global_detection
                    else None
                ),
                "last_global_success_age_s": (
                    round(now - self._last_global_success, 1)
                    if self._last_global_success
                    else None
                ),
                "tweets_session": self._tweet_count_session,
                "tweets_24h": len(self._tweet_count_24h),
                "lifetime_count": self._lifetime_count,
                "lifetime_count_source": self._lifetime_count_source,
                "lifetime_count_age_s": (
                    round(now - self._lifetime_count_ts, 1)
                    if self._lifetime_count_ts
                    else None
                ),
                "sources": sources,
                "restart_count": dict(self._restart_count),
                "recent_alerts": [
                    {
                        "ts": a.ts,
                        "level": a.level,
                        "code": a.code,
                        "message": a.message,
                        "source": a.source,
                    }
                    for a in list(self._alerts)[-10:]
                ],
            }

    def write_to_file(self, path: str) -> None:
        snap = self.snapshot()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, default=str)
            import os
            os.replace(tmp, path)
        except Exception:
            pass

    # ── shutdown signal ──────────────────────────────────────────────────

    def request_stop(self) -> None:
        with self._lock:
            self._stop_requested = True

    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested
