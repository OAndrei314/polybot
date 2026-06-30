"""
XTracker backup poller.

xtracker.polymarket.com is Polymarket's own tweet-count tracker. It polls
Twitter (or a downstream source) independently of us and exposes the
lifetime tweet count via a public REST API. We use it as:

  1. Backup count source — even if all our Twitter accounts die, we still
     know what Polymarket's market is reacting to.
  2. Divergence detector — if XTracker's count > our GQL count for >N
     seconds, our Twitter pipeline is silently broken (probably stale
     hash or dead accounts) and we should alert.

XTracker lag is typically 15-60s, so it is NEVER the fastest signal —
but it IS the most reliable. Treat it as the floor.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

from signals.health_state import HealthState


XTRACKER_URL = (
    "https://xtracker.polymarket.com/api/users/{username}/trackings"
    "?activeOnly=false"
)
USERNAME_DEFAULT = "elonmusk"
SOURCE_NAME = "xtracker"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _walk_for_count(obj) -> Optional[int]:
    """Find the first integer 'tweetsCount' / 'tweets_count' / 'count' in payload."""
    if isinstance(obj, dict):
        for k in ("tweetsCount", "tweets_count", "tweetCount", "tweet_count", "count"):
            v = obj.get(k)
            if isinstance(v, (int, float)) and v > 1000:  # plausibility floor
                return int(v)
        for v in obj.values():
            r = _walk_for_count(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_for_count(v)
            if r is not None:
                return r
    return None


class XTrackerPoller:
    """
    Polls XTracker every `interval_s` seconds and reports the lifetime
    tweet count to HealthState.

    If the count it reports is HIGHER than our recorded GQL lifetime count
    for more than `divergence_alert_s` seconds, fires a divergence alert
    via HealthState.alert(...).
    """

    def __init__(
        self,
        health: HealthState,
        username: str = USERNAME_DEFAULT,
        interval_s: float = 30.0,
        timeout_s: float = 8.0,
        divergence_alert_s: float = 180.0,
        log: Optional[logging.Logger] = None,
    ):
        self.health = health
        self.username = username
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.divergence_alert_s = divergence_alert_s
        self.log = log or logging.getLogger("xtracker_poll")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._divergence_since: Optional[float] = None
        self._last_xtracker_count: Optional[int] = None
        self._last_xtracker_at: float = 0.0
        self._session = requests.Session()
        self._session.headers["User-Agent"] = _UA

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def state(self) -> dict:
        return {
            "last_count": self._last_xtracker_count,
            "last_count_age_s": (
                round(time.time() - self._last_xtracker_at, 1)
                if self._last_xtracker_at else None
            ),
            "divergence_since": self._divergence_since,
        }

    def _check_divergence(self, xt_count: int) -> None:
        """Compare against HealthState's lifetime count from GQL/REST sources."""
        our_count, our_source, our_ts = self.health.lifetime_count()
        now = time.time()
        # only compare if we have an "our" count from a non-xtracker source
        if our_count is None or our_source == SOURCE_NAME:
            self._divergence_since = None
            return
        # If our count is stale (> 5 min), don't trust it for divergence check —
        # could be that GQL is just slow, not broken
        if now - our_ts > 300:
            return

        diff = xt_count - our_count
        if diff >= 2:
            if self._divergence_since is None:
                self._divergence_since = now
            elapsed = now - self._divergence_since
            if elapsed > self.divergence_alert_s:
                self.health.alert(
                    level="critical",
                    code="xtracker_divergence",
                    message=(
                        f"XTracker count ({xt_count}) > our GQL count ({our_count}, "
                        f"src={our_source}) by {diff} for {int(elapsed)}s — "
                        f"Twitter pipeline likely silently broken"
                    ),
                    source=SOURCE_NAME,
                    dedup_s=600.0,
                )
        else:
            self._divergence_since = None

    def _poll_once(self) -> None:
        try:
            r = self._session.get(
                XTRACKER_URL.format(username=self.username),
                timeout=self.timeout_s,
            )
        except Exception as e:
            self.health.mark_error(SOURCE_NAME, f"net:{e}")
            return

        if r.status_code != 200:
            self.health.mark_error(SOURCE_NAME, f"http:{r.status_code}")
            return

        try:
            data = r.json()
        except Exception:
            self.health.mark_error(SOURCE_NAME, "bad_json")
            return

        count = _walk_for_count(data)
        if count is None:
            self.health.mark_error(SOURCE_NAME, "no_count")
            return

        self._last_xtracker_count = count
        self._last_xtracker_at = time.time()
        self.health.mark_success(SOURCE_NAME, lifetime_count=count)
        self._check_divergence(count)

    def _run(self) -> None:
        self.health.register(SOURCE_NAME)
        while not self._stop.is_set():
            self.health.heartbeat(SOURCE_NAME)
            try:
                self._poll_once()
            except Exception as e:
                self.log.exception("xtracker poll crashed: %s", e)
                self.health.mark_error(SOURCE_NAME, f"crash:{e}")
            # sleep responsive to stop
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="xtracker_poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
