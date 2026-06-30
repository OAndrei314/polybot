"""
State checkpoint to disk so the watch process can restart without losing context.

What is persisted:
  - seen_tweet_ids: ids of tweets the dedup has already emitted
  - lifetime_count: last reported count
  - dead_accounts: which accounts have been benched (so we don't try them again)
  - hashes: most recent GQL queryIds
  - started_at: original start (for restart detection)

Write strategy: atomic rename (write to .tmp, then os.replace).
Read strategy: best-effort, ignore corruption.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional


DEFAULT_PATH = "data/watch_state.json"
# A tweet older than this is irrelevant — we drop it from the seen-set on save
# to keep file size bounded.
SEEN_TTL_S = 86400.0 * 3  # 3 days


class Checkpoint:
    """
    Periodic state writer.

    Use:
      ckpt = Checkpoint(path="data/watch_state.json",
                        get_state_fn=lambda: {...})
      ckpt.start()                # spawns background writer thread
      restored = ckpt.load()      # call ONCE at startup before start()
    """

    def __init__(
        self,
        get_state_fn,
        path: str = DEFAULT_PATH,
        interval_s: float = 30.0,
        log: Optional[logging.Logger] = None,
    ):
        self.get_state_fn = get_state_fn
        self.path = path
        self.interval_s = interval_s
        self.log = log or logging.getLogger("checkpoint")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except Exception:
            pass

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            self.log.warning("checkpoint load failed (%s); starting fresh", e)
        return {}

    def save_now(self) -> bool:
        try:
            state = self.get_state_fn() or {}
            # prune old seen ids
            seen = state.get("seen_tweet_ids")
            if isinstance(seen, dict):
                # values are last-seen timestamps
                cutoff = time.time() - SEEN_TTL_S
                state["seen_tweet_ids"] = {
                    k: v for k, v in seen.items()
                    if isinstance(v, (int, float)) and v >= cutoff
                }
            state["_saved_at"] = time.time()
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, self.path)
            return True
        except Exception:
            self.log.exception("checkpoint save failed")
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            self.save_now()
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="checkpoint", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # save one final time before stopping
        self._stop.set()
        try:
            self.save_now()
        except Exception:
            pass
