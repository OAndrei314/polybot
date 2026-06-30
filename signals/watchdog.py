"""
Watchdog for worker threads.

Registers a set of (name, factory) pairs where `factory()` spawns a new
daemon thread for that worker. Every check_interval seconds:

  - For each registered worker:
      * If is_alive() == False  → restart and increment HealthState restart count.
      * If heartbeat_age > max_heartbeat_age_s → log and restart (zombie).
  - Optionally invokes a "throttle hook" callback every tick so callers can
    apply hard-cap throttling (e.g. slow down polling if all accounts are
    near rate-limit exhaustion).

Usage:
  wd = ThreadWatchdog(health)
  wd.register("gql_poll", factory=lambda: spawn_gql_poll_thread(),
              max_heartbeat_age_s=60)
  wd.register("xtracker", factory=lambda: xtracker.start(),
              max_heartbeat_age_s=120)
  wd.start()
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from signals.health_state import HealthState


@dataclass
class _Worker:
    name: str
    factory: Callable[[], threading.Thread]
    max_heartbeat_age_s: float
    thread: Optional[threading.Thread] = None
    last_restart: float = 0.0
    restart_cooldown_s: float = 10.0  # don't thrash-restart


class ThreadWatchdog:
    def __init__(
        self,
        health: HealthState,
        check_interval_s: float = 5.0,
        throttle_hook: Optional[Callable[[], None]] = None,
        log: Optional[logging.Logger] = None,
    ):
        self.health = health
        self.check_interval_s = check_interval_s
        self.throttle_hook = throttle_hook
        self.log = log or logging.getLogger("watchdog")
        self._workers: dict[str, _Worker] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register(
        self,
        name: str,
        factory: Callable[[], threading.Thread],
        max_heartbeat_age_s: float = 60.0,
        restart_cooldown_s: float = 10.0,
    ) -> None:
        with self._lock:
            self._workers[name] = _Worker(
                name=name,
                factory=factory,
                max_heartbeat_age_s=max_heartbeat_age_s,
                restart_cooldown_s=restart_cooldown_s,
            )

    def _restart(self, w: _Worker, reason: str) -> None:
        now = time.time()
        if now - w.last_restart < w.restart_cooldown_s:
            return
        self.log.warning("watchdog: restarting worker %s (%s)", w.name, reason)
        w.last_restart = now
        try:
            t = w.factory()
            if isinstance(t, threading.Thread):
                w.thread = t
            self.health.mark_restart(w.name)
        except Exception:
            self.log.exception("factory for %s raised", w.name)

    def _check_once(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            alive = w.thread is not None and w.thread.is_alive()
            heartbeat_age = self.health.heartbeat_age(w.name)
            if not alive:
                self._restart(w, "thread_dead")
                continue
            if heartbeat_age != float("inf") and heartbeat_age > w.max_heartbeat_age_s:
                self.log.warning(
                    "watchdog: %s zombie (heartbeat_age=%.1fs > %.1fs)",
                    w.name, heartbeat_age, w.max_heartbeat_age_s,
                )
                # don't try to kill the zombie — just respawn (old thread is daemon)
                self._restart(w, f"heartbeat_age={heartbeat_age:.0f}s")
        if self.throttle_hook:
            try:
                self.throttle_hook()
            except Exception:
                self.log.exception("throttle hook crashed")

    def _run(self) -> None:
        # Initial spawn of all registered workers
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            try:
                t = w.factory()
                if isinstance(t, threading.Thread):
                    w.thread = t
            except Exception:
                self.log.exception("initial spawn of %s failed", w.name)

        while not self._stop.is_set():
            try:
                self._check_once()
            except Exception:
                self.log.exception("watchdog tick crashed")
            self._stop.wait(self.check_interval_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


class HardCapThrottle:
    """
    Sits as a `throttle_hook` for the watchdog.

    Reads per-account remaining-budget from the GQL rotator. If ALL live
    accounts are below `slow_threshold_rem`, raises min_interval to the
    `slow_min_interval_s` value until they recover.
    """

    def __init__(
        self,
        rotator,
        normal_min_interval_s: float = 6.5,
        slow_min_interval_s: float = 15.0,
        slow_threshold_rem: int = 10,
        log: Optional[logging.Logger] = None,
    ):
        self.rotator = rotator
        self.normal = normal_min_interval_s
        self.slow = slow_min_interval_s
        self.threshold = slow_threshold_rem
        self.log = log or logging.getLogger("throttle")
        self._in_slow_mode = False

    def __call__(self) -> None:
        st = self.rotator.status()
        budget = st.get("budget", {})
        if not budget:
            return
        live_rems = []
        for label, b in budget.items():
            if b.get("dead"):
                continue
            rem = b.get("remaining")
            if rem is None:
                # never seen rem — be cautious, treat as "high enough"
                live_rems.append(999)
            else:
                live_rems.append(int(rem))
        if not live_rems:
            return
        all_low = all(r <= self.threshold for r in live_rems)
        if all_low and not self._in_slow_mode:
            self.log.warning(
                "throttle: ALL accounts below rem=%d, slowing to %.1fs",
                self.threshold, self.slow,
            )
            self.rotator._min_interval_s = self.slow
            self._in_slow_mode = True
        elif (not all_low) and self._in_slow_mode and max(live_rems) >= self.threshold * 3:
            self.log.info(
                "throttle: budget recovered, returning to %.1fs", self.normal,
            )
            self.rotator._min_interval_s = self.normal
            self._in_slow_mode = False
