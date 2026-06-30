"""
Auto GQL query-hash refresher.

Twitter rotates the GQL query hashes (UserTweets, UserByScreenName, etc)
whenever they redeploy their web client. When that happens, all polling
requests start returning 404 and the bot goes silent.

This module scrapes Twitter's main.js bundle, extracts the current hashes,
and exposes them to pollers. Runs on a background thread, refreshes every
6h by default and on demand when a 404 is reported.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

import requests


_HOME_URL = "https://x.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_BUNDLE_RE = re.compile(
    r"https://abs\.twimg\.com/responsive-web/client-web/[^\s\"']+\.js"
)
# Known operation names we care about and the regex used to extract their queryId
_OPS = (
    "UserTweets",
    "HomeLatestTimeline",
    "UserByScreenName",
    "UserByRestId",
)


def _extract_hashes(js_text: str, ops: tuple[str, ...]) -> dict[str, str]:
    """Find {operationName: queryId} mappings in a JS bundle."""
    out: dict[str, str] = {}
    for op in ops:
        # patterns Twitter uses:
        #   queryId:"HASH",operationName:"OP"
        #   operationName:"OP",queryId:"HASH"
        m = re.search(rf'queryId:"([^"]+)",operationName:"{op}"', js_text)
        if not m:
            m = re.search(rf'operationName:"{op}"[^}}]*?queryId:"([^"]+)"', js_text)
        if m:
            out[op] = m.group(1)
    return out


def fetch_current_hashes(
    ops: tuple[str, ...] = _OPS,
    timeout_s: float = 15.0,
) -> dict[str, str]:
    """
    Synchronously fetch the current GQL hashes from Twitter's JS bundle.
    Returns dict of {operation_name: query_id}. Empty dict on failure.
    """
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    try:
        r = s.get(_HOME_URL, timeout=timeout_s)
        if r.status_code != 200:
            return {}
        bundles = _BUNDLE_RE.findall(r.text)
    except Exception:
        return {}

    needed = set(ops)
    found: dict[str, str] = {}
    # Prefer main.* first; iterate all if needed
    bundles_sorted = sorted(bundles, key=lambda u: ("main." not in u, u))
    for url in bundles_sorted:
        if not needed:
            break
        try:
            rb = s.get(url, timeout=timeout_s)
            if rb.status_code != 200:
                continue
            found_here = _extract_hashes(rb.text, tuple(needed))
            for op, qid in found_here.items():
                found[op] = qid
                needed.discard(op)
        except Exception:
            continue
    return found


class HashRegistry:
    """
    Thread-safe registry of current GQL query hashes.

    Use:
      reg = HashRegistry(initial={"UserTweets": "36rb3...", ...})
      reg.start()                       # spawns background refresh thread
      qid = reg.get("UserTweets")       # current hash
      reg.notify_404("UserTweets")      # trigger immediate refresh
      reg.stop()
    """

    def __init__(
        self,
        initial: Optional[dict[str, str]] = None,
        refresh_interval_s: float = 6 * 3600.0,
        on_change=None,
        log: Optional[logging.Logger] = None,
        ops: tuple[str, ...] = _OPS,
    ):
        self._hashes: dict[str, str] = dict(initial or {})
        self._refresh_interval_s = refresh_interval_s
        self._on_change = on_change  # callback(operation, old_hash, new_hash)
        self._ops = ops
        self.log = log or logging.getLogger("hash_refresh")
        self._lock = threading.Lock()
        self._refresh_now = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_refresh_at: float = 0.0
        self._last_refresh_ok: bool = False
        self._refresh_count = 0
        self._change_count = 0

    def get(self, op: str) -> Optional[str]:
        with self._lock:
            return self._hashes.get(op)

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._hashes)

    def notify_404(self, op: str = "") -> None:
        """Call when a poller observes 404. Triggers an immediate refresh."""
        self.log.warning("notify_404(op=%s) — triggering hash refresh", op or "*")
        self._refresh_now.set()

    def status(self) -> dict:
        with self._lock:
            return {
                "hashes": dict(self._hashes),
                "last_refresh_at": self._last_refresh_at,
                "last_refresh_ok": self._last_refresh_ok,
                "refresh_count": self._refresh_count,
                "change_count": self._change_count,
            }

    def _do_refresh(self) -> bool:
        latest = fetch_current_hashes(self._ops)
        ok = bool(latest)
        changes: list[tuple[str, str, str]] = []
        with self._lock:
            self._last_refresh_at = time.time()
            self._last_refresh_ok = ok
            self._refresh_count += 1
            if ok:
                for op, qid in latest.items():
                    old = self._hashes.get(op)
                    if old != qid:
                        changes.append((op, old or "", qid))
                        self._hashes[op] = qid
                        self._change_count += 1
        for op, old, new in changes:
            self.log.warning("GQL hash changed: %s  %s -> %s", op, old, new)
            if self._on_change:
                try:
                    self._on_change(op, old, new)
                except Exception:
                    self.log.exception("on_change callback failed")
        return ok

    def _run(self) -> None:
        # Initial refresh immediately (so we don't depend on stale code constants)
        try:
            self._do_refresh()
        except Exception:
            self.log.exception("initial hash refresh crashed")

        while not self._stop.is_set():
            # Wait for either: scheduled interval, or notify_404 trigger
            fired = self._refresh_now.wait(timeout=self._refresh_interval_s)
            if self._stop.is_set():
                break
            self._refresh_now.clear()
            try:
                self._do_refresh()
            except Exception:
                self.log.exception("scheduled hash refresh crashed")
            # If we just refreshed because of a notify, debounce a bit so a
            # flood of 404s doesn't hammer Twitter's main page.
            if fired:
                self._stop.wait(30.0)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hash_refresh", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._refresh_now.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
