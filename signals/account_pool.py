"""
Multi-account Twitter credential pool for parallel SSE + GraphQL tweet detection.

Credential loading (in priority order):
  1. accounts.json file in the project root — JSON array of account objects
  2. TWITTER_ACCOUNTS env var — same JSON array format, inline
  3. Numbered env vars: TWITTER_BEARER_1 / TWITTER_COOKIE_1 / TWITTER_CSRF_1 ...
  4. Legacy single:     TWITTER_BEARER  / TWITTER_COOKIE  / TWITTER_CSRF

accounts.json format:
  [
    {"bearer": "AAAAAAAAAAAAAAAAAAAAAxx...", "cookie": "auth_token=...", "csrf": "abc123"},
    {"bearer": "AAAAAAAAAAAAAAAAAAAAAyy...", "cookie": "auth_token=...", "csrf": "def456"},
    ...
  ]
  Optional extra fields per entry: "label" (string name for logs).

SSE connections stay alive for all accounts — they are passive long-lived streams
and the ban risk is minimal. GraphQL polling uses AccountRotator, which round-robins
through accounts and puts individual ones on cooldown after 429s or errors.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


ELON_USER_ID = "44196397"

# Looked-up relative to this file's parent directory (project root).
_ACCOUNTS_FILE = Path(__file__).resolve().parent.parent / "accounts.json"


@dataclass
class Account:
    bearer: str
    cookie: str
    csrf: str
    label: str = ""

    def __post_init__(self):
        if self.bearer and not self.bearer.lower().startswith("bearer "):
            self.bearer = f"Bearer {self.bearer}"

    def is_valid(self) -> bool:
        return bool(self.bearer and self.cookie)


def _parse_entries(entries: list) -> list[Account]:
    accounts: list[Account] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        a = Account(
            bearer=str(e.get("bearer") or e.get("BEARER") or "").strip(),
            cookie=str(e.get("cookie") or e.get("COOKIE") or "").strip(),
            csrf=str(e.get("csrf") or e.get("CSRF") or "").strip(),
            label=str(e.get("label") or f"acct{i}"),
        )
        if a.is_valid():
            accounts.append(a)
    return accounts


def load_accounts(accounts_file: str | Path | None = None) -> list[Account]:
    """Return all valid Twitter accounts from any configured source."""
    # 1. accounts.json file
    path = Path(accounts_file) if accounts_file else _ACCOUNTS_FILE
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(entries, list):
                accounts = _parse_entries(entries)
                if accounts:
                    return accounts
        except Exception:
            pass

    # 2. TWITTER_ACCOUNTS env var (JSON array)
    raw = os.getenv("TWITTER_ACCOUNTS", "").strip()
    if raw:
        try:
            entries = json.loads(raw)
            if isinstance(entries, list):
                accounts = _parse_entries(entries)
                if accounts:
                    return accounts
        except Exception:
            pass

    # 3. Numbered vars TWITTER_BEARER_N / TWITTER_COOKIE_N / TWITTER_CSRF_N
    accounts: list[Account] = []
    for i in range(1, 200):
        bearer = os.getenv(f"TWITTER_BEARER_{i}", "").strip()
        cookie = os.getenv(f"TWITTER_COOKIE_{i}", "").strip()
        if not bearer or not cookie:
            if i > 1:
                break
            continue
        csrf = os.getenv(f"TWITTER_CSRF_{i}", "").strip()
        accounts.append(Account(bearer=bearer, cookie=cookie, csrf=csrf, label=f"acct{i}"))
    if accounts:
        return accounts

    # 4. Legacy single-account vars
    bearer = os.getenv("TWITTER_BEARER", "").strip()
    cookie = os.getenv("TWITTER_COOKIE", "").strip()
    csrf = os.getenv("TWITTER_CSRF", "").strip()
    if bearer and cookie:
        return [Account(bearer=bearer, cookie=cookie, csrf=csrf, label="acct0")]

    return []


class AccountRotator:
    """
    Round-robin account selector with per-account rate-limit budgeting.

    Two protections prevent 429s:
      1. min_interval_s — each account waits this long between picks.
         Default 6.5s = ~9.2 req/min/account, safely under Twitter's
         ~10 req/min/account GQL UserTweets budget (150/15min).
      2. cool_until — explicit cooldowns from 429 responses or errors.

    Math (N accounts, min_interval=6.5s):
      Effective poll interval = 6.5 / N seconds.
      With 2 accounts → poll every 3.25s, each account every 6.5s.
      With 3 accounts → poll every 2.17s, each account every 6.5s.
      With 5 accounts → poll every 1.3s, each account every 6.5s.
      Latency target = effective_interval / 2.

    Rate-limit headers are honored when present (x-rate-limit-remaining,
    x-rate-limit-reset).
    """

    def __init__(
        self,
        accounts: list[Account],
        cooldown_429_s: float = 300.0,
        cooldown_error_s: float = 30.0,
        min_interval_s: float = 6.5,
        jitter_s: float = 1.5,
        initial_stagger_s: float = 0.0,
        global_min_interval_s: float | None = None,
        rate_limit_slack_s: float = 2.0,
        dead_after_consecutive_401: int = 3,
        on_account_dead=None,   # callable(label, reason)
    ):
        import random as _random
        self._accounts = list(accounts)
        self._cooldown_429_s = cooldown_429_s
        self._cooldown_error_s = cooldown_error_s
        self._min_interval_s = min_interval_s
        self._jitter_s = max(0.0, jitter_s)
        self._global_min_interval_s = global_min_interval_s
        self._rate_limit_slack_s = max(0.0, rate_limit_slack_s)
        self._dead_after_consecutive_401 = dead_after_consecutive_401
        self._on_account_dead = on_account_dead
        self._rng = _random.Random()
        self._lock = threading.Lock()
        self._cool_until: dict[str, float] = {}
        self._last_used: dict[str, float] = {}
        self._last_status: dict[str, str] = {}  # label → "ok" / "429" / "err:403" etc.
        self._remaining: dict[str, int] = {}   # latest x-rate-limit-remaining seen
        self._reset_at: dict[str, float] = {}  # latest x-rate-limit-reset (unix)
        self._budget_ready_at: dict[str, float] = {}  # paced next request from rate-limit headers
        self._budget_gap_s: dict[str, float] = {}      # latest computed reset/rem gap
        self._consecutive_401: dict[str, int] = {}
        self._dead: set[str] = set()             # accounts permanently benched
        self._dead_reason: dict[str, str] = {}
        self._lane_next_at: float = 0.0
        self._last_lane_interval_s: float = 0.0
        # Per-account jitter applied to the NEXT gap. Re-sampled each pick.
        self._next_jitter: dict[str, float] = {}
        # Seed a phase offset so the first cycle is evenly spaced instead of
        # burning through every account in one startup burst.
        if initial_stagger_s > 0:
            now = time.time()
            for i, a in enumerate(self._accounts):
                self._last_used[a.label] = now - self._min_interval_s + i * initial_stagger_s

    def _ready_at(self, a: Account, now: float) -> float:
        """Time at which account `a` can next be polled. Dead = never."""
        if a.label in self._dead:
            return float("inf")
        jitter = self._next_jitter.get(a.label, 0.0)
        return max(
            self._cool_until.get(a.label, 0.0),
            self._budget_ready_at.get(a.label, 0.0),
            self._last_used.get(a.label, 0.0) + self._min_interval_s + jitter,
        )

    def _account_cycle_gap_s(self, a: Account) -> float:
        """Estimated sustainable per-account gap used for lane pacing."""
        min_gap = self._min_interval_s + (self._jitter_s / 2.0)
        budget_gap = self._budget_gap_s.get(a.label)
        if isinstance(budget_gap, (int, float)) and budget_gap > 0:
            return max(min_gap, float(budget_gap))
        return max(0.05, min_gap)

    def _aggregate_interval_s(self, now: float) -> float:
        """
        Sustainable interval between any two requests in this lane.

        If accounts have cycle gaps [18s, 18s, 18s, 18s], the lane should emit
        about once every 4.5s, not four requests in the same second.
        """
        live = [
            a for a in self._accounts
            if a.label not in self._dead and now >= self._cool_until.get(a.label, 0.0)
        ]
        if not live:
            return 5.0
        total_rate = 0.0
        for a in live:
            gap = self._account_cycle_gap_s(a)
            if gap > 0:
                total_rate += 1.0 / gap
        if total_rate <= 0:
            dynamic = 5.0
        else:
            dynamic = 1.0 / total_rate
        if self._global_min_interval_s is not None:
            dynamic = max(dynamic, self._global_min_interval_s)
        return max(0.05, dynamic)

    def _mark_picked(self, label: str, now: float) -> None:
        """Record a pick and roll a fresh jitter for the NEXT gap."""
        self._last_used[label] = now
        # uniform jitter in [0, jitter_s) — each account's gaps are now non-periodic
        if self._jitter_s > 0:
            self._next_jitter[label] = self._rng.uniform(0.0, self._jitter_s)
        else:
            self._next_jitter[label] = 0.0
        lane_interval = self._aggregate_interval_s(now)
        self._last_lane_interval_s = lane_interval
        self._lane_next_at = now + lane_interval

    def _enforce_lane_interval(self, now: float) -> None:
        """After fresh rate-limit headers, keep the next lane tick conservative."""
        lane_interval = self._aggregate_interval_s(now)
        self._last_lane_interval_s = lane_interval
        self._lane_next_at = max(self._lane_next_at, now + lane_interval)

    def _live_accounts(self):
        return [a for a in self._accounts if a.label not in self._dead]

    def next(self) -> Account | None:
        """Return the account ready to poll right now, or None if all are waiting."""
        now = time.time()
        with self._lock:
            if now < self._lane_next_at:
                return None
            live = self._live_accounts()
            ready = [a for a in live if now >= self._ready_at(a, now)]
            if not ready:
                return None
            chosen = min(ready, key=lambda a: self._last_used.get(a.label, 0.0))
            self._mark_picked(chosen.label, now)
            return chosen

    def wait_and_get(self, stop_evt: threading.Event, heartbeat=None) -> Account | None:
        """
        Block until an account is ready, then return it.
        Keeps looping if all accounts are dead — they may be revived later
        (manual cookie refresh, etc). Returns None only when stop_evt fires.

        `heartbeat` is an optional zero-arg callback fired before each wait
        cycle so the watchdog can see liveness even when all accounts are
        cooling for an extended period.
        """
        while not stop_evt.is_set():
            if heartbeat is not None:
                try:
                    heartbeat()
                except Exception:
                    pass
            now = time.time()
            with self._lock:
                live = self._live_accounts()
                if not live:
                    wait_s = 5.0
                else:
                    lane_wait = max(0.0, self._lane_next_at - now)
                    ready = [
                        a for a in live
                        if lane_wait <= 0.0 and now >= self._ready_at(a, now)
                    ]
                    if ready:
                        chosen = min(ready, key=lambda a: self._last_used.get(a.label, 0.0))
                        self._mark_picked(chosen.label, now)
                        return chosen
                    earliest = min(self._ready_at(a, now) for a in live)
                    wait_s = max(0.05, lane_wait, earliest - now)
            stop_evt.wait(min(wait_s, 5.0))
        return None

    def mark_account_dead(self, label: str, reason: str = "manual") -> None:
        """Permanently bench an account (cookie expired, suspended, etc)."""
        fire = False
        with self._lock:
            if label not in self._dead:
                self._dead.add(label)
                self._dead_reason[label] = reason
                self._last_status[label] = f"DEAD:{reason}"
                fire = True
        if fire and self._on_account_dead:
            try:
                self._on_account_dead(label, reason)
            except Exception:
                pass

    def revive_account(self, label: str) -> None:
        """Bring a dead account back (e.g. after manual cookie refresh)."""
        with self._lock:
            self._dead.discard(label)
            self._dead_reason.pop(label, None)
            self._consecutive_401[label] = 0
            self._cool_until.pop(label, None)
            self._budget_ready_at.pop(label, None)
            self._budget_gap_s.pop(label, None)

    def has_live_accounts(self) -> bool:
        with self._lock:
            return bool(self._live_accounts())

    def live_count(self) -> int:
        with self._lock:
            return len(self._live_accounts())

    def mark_ok(self, label: str) -> None:
        with self._lock:
            self._last_status[label] = "ok"

    def _apply_rate_limit_pacing(self, label: str, remaining: int, reset_at: float, now: float) -> None:
        """
        Spread an account's remaining budget evenly until the reset.

        Example: remaining=5 and reset_in=750s => next budget-eligible poll
        is about 150s from now. This preserves uptime instead of spending
        the last few requests in a burst and then going dark.
        """
        self._remaining[label] = remaining
        self._reset_at[label] = reset_at
        reset_in = reset_at - now
        if reset_in <= 0:
            self._budget_ready_at.pop(label, None)
            self._budget_gap_s.pop(label, None)
            return
        if remaining <= 0:
            self._budget_ready_at[label] = reset_at + self._rate_limit_slack_s
            self._budget_gap_s[label] = reset_in + self._rate_limit_slack_s
            self._last_status[label] = f"budget_empty (resets in {round(reset_in)}s)"
            self._maybe_apply_phase_nudge(label, remaining, reset_at, now)
            return
        gap = (reset_in + self._rate_limit_slack_s) / max(1, remaining)
        self._budget_gap_s[label] = gap
        self._budget_ready_at[label] = now + gap
        self._last_status[label] = f"ok (rem={remaining}, pace={gap:.1f}s)"
        self._maybe_apply_phase_nudge(label, remaining, reset_at, now)

    # Phase-desync constants. When N accounts cycle through identical 15-min
    # reset windows in lockstep, the lane is one correlated-failure event away
    # from a coverage gap. The nudger gradually drifts each account's reset_at
    # forward when it shares a window with a peer.
    _PHASE_SYNC_THRESHOLD_S = 200.0  # treat as "synced" if reset_at within this
    _PHASE_NUDGE_S = 60.0            # shift per cycle, applied to the later peer
    _PHASE_NUDGE_LOW_REM = 5         # only nudge when account is near empty

    def _maybe_apply_phase_nudge(
        self, label: str, remaining: int, reset_at: float, now: float
    ) -> None:
        if remaining > self._PHASE_NUDGE_LOW_REM:
            return
        label_to_idx = {a.label: i for i, a in enumerate(self._accounts)}
        my_idx = label_to_idx.get(label)
        if my_idx is None:
            return
        synced: list[tuple[int, str]] = []
        for other_label, other_reset in self._reset_at.items():
            if other_label == label or other_label in self._dead:
                continue
            other_idx = label_to_idx.get(other_label)
            if other_idx is None:
                continue
            if abs(other_reset - reset_at) <= self._PHASE_SYNC_THRESHOLD_S:
                synced.append((other_idx, other_label))
        if not synced:
            return
        # Rank within the synced cluster (including self). Rank 0 = earliest by
        # account index, gets no nudge. Each subsequent peer is offset by an
        # additional PHASE_NUDGE_S so they separate from each other, not just
        # from the cluster anchor.
        cluster_indices = sorted([my_idx] + [idx for idx, _ in synced])
        my_rank = cluster_indices.index(my_idx)
        if my_rank == 0:
            return
        nudge_seconds = self._PHASE_NUDGE_S * my_rank
        nudge_until = reset_at + nudge_seconds
        if nudge_until <= self._budget_ready_at.get(label, 0.0):
            return
        self._budget_ready_at[label] = nudge_until
        self._cool_until[label] = max(self._cool_until.get(label, 0.0), nudge_until)
        peers = ",".join(l for _, l in synced)
        self._last_status[label] = (
            f"phase_nudge (+{nudge_seconds:.0f}s past reset, rank={my_rank}, synced with {peers})"
        )

    def mark_response(self, label: str, status: int, headers=None) -> None:
        """
        Update state from a real HTTP response.
        Reads x-rate-limit-* headers when present to preemptively cool
        accounts that are running low on budget.

        Three consecutive 401s permanently bench the account (cookie expired
        or revoked). The on_account_dead callback fires once.
        """
        now = time.time()
        fire_dead = None
        with self._lock:
            # reset 401 counter on any non-401 status
            if status != 401:
                self._consecutive_401[label] = 0

            if status == 200:
                self._last_status[label] = "ok"
                # parse rate-limit headers
                try:
                    if headers is not None:
                        rem_s = headers.get("x-rate-limit-remaining") or headers.get("X-Rate-Limit-Remaining")
                        rst_s = headers.get("x-rate-limit-reset") or headers.get("X-Rate-Limit-Reset")
                        if rem_s is not None:
                            rem = int(rem_s)
                            if rst_s is not None:
                                reset_at = float(rst_s)
                                self._apply_rate_limit_pacing(label, rem, reset_at, now)
                            else:
                                self._remaining[label] = rem
                                self._last_status[label] = f"ok (rem={rem})"
                except Exception:
                    pass
                self._enforce_lane_interval(now)
            elif status == 429:
                # Honor x-rate-limit-reset when present (Twitter's authoritative answer).
                # Floor at 30s to avoid spamming if the header is missing/garbage.
                cool_until = None
                try:
                    if headers is not None:
                        rst_s = headers.get("x-rate-limit-reset") or headers.get("X-Rate-Limit-Reset")
                        if rst_s is not None:
                            reset_at = float(rst_s)
                            self._reset_at[label] = reset_at
                            if reset_at > now + 5:  # sanity: header must be in the future
                                cool_until = reset_at + 1.0
                except Exception:
                    pass
                if cool_until is None:
                    cool_until = now + self._cooldown_429_s
                self._cool_until[label] = cool_until
                self._budget_ready_at[label] = cool_until
                self._enforce_lane_interval(now)
                self._last_status[label] = "429"
            elif status == 401:
                self._consecutive_401[label] = self._consecutive_401.get(label, 0) + 1
                self._cool_until[label] = now + self._cooldown_error_s
                self._last_status[label] = f"401 ({self._consecutive_401[label]}x)"
                if self._consecutive_401[label] >= self._dead_after_consecutive_401:
                    if label not in self._dead:
                        self._dead.add(label)
                        self._dead_reason[label] = "auth_expired"
                        self._last_status[label] = "DEAD:auth_expired"
                        fire_dead = label
            else:
                self._cool_until[label] = now + self._cooldown_error_s
                self._last_status[label] = f"err:{status}"
        if fire_dead and self._on_account_dead:
            try:
                self._on_account_dead(fire_dead, "auth_expired")
            except Exception:
                pass

    def mark_429(self, label: str) -> None:
        with self._lock:
            self._cool_until[label] = time.time() + self._cooldown_429_s
            self._budget_ready_at[label] = self._cool_until[label]
            self._last_status[label] = "429"

    def mark_error(self, label: str, status_code: int = 0) -> None:
        with self._lock:
            self._cool_until[label] = time.time() + self._cooldown_error_s
            self._last_status[label] = f"err:{status_code}" if status_code else "err"

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            lane_wait_s = max(0.0, self._lane_next_at - now)
            account_ready = sum(
                1 for a in self._accounts
                if a.label not in self._dead and now >= self._ready_at(a, now)
            )
            live_count = sum(1 for a in self._accounts if a.label not in self._dead)
            active = account_ready if lane_wait_s <= 0.0 else 0
            waiting_count = max(0, live_count - active)
            cooling = [
                {
                    "label": a.label,
                    "resumes_in_s": round(max(self._ready_at(a, now), self._lane_next_at) - now, 1),
                    "reason": self._last_status.get(a.label, "?"),
                }
                for a in self._accounts
                if a.label not in self._dead and (
                    now < self._ready_at(a, now) or lane_wait_s > 0.0
                )
            ]
            # per-account budget info
            budget = {
                a.label: {
                    "remaining": self._remaining.get(a.label),
                    "resets_in_s": (
                        round(self._reset_at[a.label] - now, 1)
                        if a.label in self._reset_at and self._reset_at[a.label] > now
                        else None
                    ),
                    "next_poll_in_s": (
                        None if a.label in self._dead
                        else round(max(0.0, max(self._ready_at(a, now), self._lane_next_at) - now), 1)
                    ),
                    "account_next_poll_in_s": (
                        None if a.label in self._dead
                        else round(max(0.0, self._ready_at(a, now) - now), 1)
                    ),
                    "lane_wait_s": round(lane_wait_s, 1),
                    "budget_pace_s": (
                        round(self._budget_gap_s[a.label], 1)
                        if a.label in self._budget_gap_s
                        else None
                    ),
                    "budget_wait_s": (
                        round(max(0.0, self._budget_ready_at[a.label] - now), 1)
                        if a.label in self._budget_ready_at
                        else None
                    ),
                    "min_wait_s": (
                        None if a.label in self._dead
                        else round(max(
                            0.0,
                            self._last_used.get(a.label, 0.0)
                            + self._min_interval_s
                            + self._next_jitter.get(a.label, 0.0)
                            - now,
                        ), 1)
                    ),
                    "status": self._last_status.get(a.label, "?"),
                    "dead": a.label in self._dead,
                    "dead_reason": self._dead_reason.get(a.label, ""),
                    "consecutive_401": self._consecutive_401.get(a.label, 0),
                }
                for a in self._accounts
            }
            dead_count = len(self._dead)
        return {
            "total": len(self._accounts),
            "active": active,
            "cooling": waiting_count,
            "dead": dead_count,
            "cooling_detail": cooling,
            "budget": budget,
            "min_interval_s": self._min_interval_s,
            "account_ready": account_ready,
            "lane_next_in_s": round(lane_wait_s, 1),
            "aggregate_interval_s": round(self._last_lane_interval_s or self._aggregate_interval_s(now), 2),
        }


class MultiSSEPool:
    """
    Spawns one DirectSSEClient per account, all watching the same user.

    First client to detect a tweet wins; duplicate tweet_ids from other
    clients are silently dropped. The on_tweet callback fires exactly once
    per tweet regardless of how many clients see it.

    SSE connections are long-lived and passive — all accounts stay connected
    permanently. A disconnect on one account does not delay detection.
    """

    def __init__(
        self,
        accounts: list[Account],
        watch_user_id: str = ELON_USER_ID,
        on_tweet: Optional[Callable[[dict], None]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self._accounts = accounts
        self._watch_user_id = watch_user_id
        self._on_tweet_cb = on_tweet
        self._on_event_cb = on_event
        self._clients: list = []
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._latest: dict = {}

    def _on_tweet(self, det: dict) -> None:
        tid = det.get("tweet_id", "")
        with self._lock:
            if tid in self._seen:
                return
            self._seen.add(tid)
            self._latest = det
        if self._on_tweet_cb:
            self._on_tweet_cb(det)

    def _on_event(self, evt: dict) -> None:
        if self._on_event_cb:
            self._on_event_cb(evt)

    def start(self) -> None:
        from signals.sse_direct import DirectSSEClient

        for i, acc in enumerate(self._accounts):
            label = acc.label or f"sse_{i}"
            client = DirectSSEClient(
                bearer=acc.bearer,
                cookie=acc.cookie,
                csrf=acc.csrf,
                watch_user_id=self._watch_user_id,
                on_tweet=self._on_tweet,
                on_event=self._on_event,
                method_label=label,
                log=logging.getLogger(f"sse_pool.{label}"),
            )
            client.start()
            self._clients.append(client)

    def stop(self) -> None:
        for client in self._clients:
            client.stop()

    def latest_tweet(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def alive_count(self) -> int:
        return sum(1 for c in self._clients if c.is_alive())

    def total_count(self) -> int:
        return len(self._clients)

    def state(self) -> dict:
        return {
            "alive": self.alive_count(),
            "total": self.total_count(),
            "seen_tweets": len(self._seen),
        }
