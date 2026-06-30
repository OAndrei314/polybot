"""
Background watcher that polls a file for "revive these accounts" signals.

When tools/refresh_cookies.py finishes refreshing an account's cookies,
it appends the account label to data/revive_accounts.txt. The running
watch process polls that file every few seconds; whenever it sees a new
label, it:

  1. Re-reads the account's cookie + csrf from accounts.json
  2. Replaces those fields on the in-memory Account object
  3. Tells the AccountRotator(s) to revive that label

This lets cookie refresh happen WITHOUT restarting the watch process,
which would lose ~30s of detection capacity.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional


class ReviveWatcher:
    def __init__(
        self,
        revive_file: str,
        accounts_file: str,
        accounts_list,         # the live list[Account] from load_accounts
        rotators,              # iterable of AccountRotator instances to revive in
        on_revive=None,        # callable(label) for alert side-effects
        interval_s: float = 5.0,
        log: Optional[logging.Logger] = None,
    ):
        self.revive_file = Path(revive_file)
        self.accounts_file = Path(accounts_file)
        self.accounts_list = accounts_list
        self.rotators = list(rotators)
        self.on_revive = on_revive
        self.interval_s = interval_s
        self.log = log or logging.getLogger("revive_watcher")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _reload_account_from_disk(self, label: str) -> bool:
        """Refresh the in-memory Account's cookie/csrf from accounts.json."""
        try:
            with open(self.accounts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log.warning("could not re-read accounts.json: %s", e)
            return False
        if not isinstance(data, list):
            return False
        fresh = next((e for e in data if isinstance(e, dict) and e.get("label") == label), None)
        if fresh is None:
            self.log.warning("revive: label %s not found on disk", label)
            return False
        # Mutate the live Account object
        live = next((a for a in self.accounts_list if a.label == label), None)
        if live is None:
            self.log.warning("revive: label %s not in live accounts", label)
            return False
        new_cookie = str(fresh.get("cookie", "")).strip()
        new_csrf = str(fresh.get("csrf", "")).strip()
        if not new_cookie or not new_csrf:
            self.log.warning("revive: refreshed entry for %s missing cookie/csrf", label)
            return False
        live.cookie = new_cookie
        live.csrf = new_csrf
        self.log.info("revive: reloaded %s (cookie=%d bytes)", label, len(new_cookie))
        return True

    def _process_pending(self) -> None:
        if not self.revive_file.exists():
            return
        try:
            with open(self.revive_file, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            # Clear the file atomically so a parallel writer doesn't re-process
            self.revive_file.write_text("", encoding="utf-8")
        except Exception as e:
            self.log.warning("could not read revive file: %s", e)
            return

        # Dedup labels in this batch
        for label in dict.fromkeys(lines):
            if self._reload_account_from_disk(label):
                for rot in self.rotators:
                    try:
                        rot.revive_account(label)
                    except Exception:
                        pass
                if self.on_revive:
                    try:
                        self.on_revive(label)
                    except Exception:
                        pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._process_pending()
            except Exception:
                self.log.exception("revive_watcher tick crashed")
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="revive_watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
