"""
Live terminal monitor — verify SSE + GQL tweet detection is working.

Usage:
    python -m signals.watch                           # watch @elonmusk
    python -m signals.watch --username latenightsatire
    python -m signals.watch --user-id 44196397
    python -m signals.watch --username latenightsatire --no-gql
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import os
import sys as _sys_early

# Force UTF-8 output on Windows so ANSI escape codes don't crash cp1252
if hasattr(_sys_early.stdout, "buffer"):
    import sys as _s
    _s.stdout = io.TextIOWrapper(_s.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests as _requests_mod

# ── resolve project root so we can import regardless of cwd ──────────────────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from signals.account_pool import load_accounts, MultiSSEPool, AccountRotator
from signals.sse_direct import snowflake_unix, ELON_USER_ID


# ── ANSI helpers ──────────────────────────────────────────────────────────────

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_WHITE  = "\033[97m"

def _c(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + _RESET

def _clear() -> None:
    print("\033[2J\033[H", end="", flush=True)


# ── user-id lookup ────────────────────────────────────────────────────────────

def lookup_user_id(username: str, accounts: list) -> Optional[str]:
    """
    Resolve a Twitter @username to its numeric user_id.

    Tries three methods in order:
      1. Public profile page HTML (no auth, works for any account)
      2. GraphQL UserByScreenName via browser session (needs credentials)
      3. Returns None if all fail — user should pass --user-id manually.
    """
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # Method 1: public profile page
    try:
        r = _requests_mod.get(
            f"https://x.com/{username}",
            headers={"user-agent": ua, "accept-language": "en-US,en;q=0.9"},
            allow_redirects=True,
            timeout=10.0,
        )
        # Look for numeric ID in the page source
        m = re.search(r'"id_str"\s*:\s*"(\d{5,})"', r.text)
        if not m:
            m = re.search(r'"userId"\s*:\s*"(\d{5,})"', r.text)
        if not m:
            m = re.search(r'"user_id"\s*:\s*"(\d{5,})"', r.text)
        if m:
            return m.group(1)
    except Exception:
        pass

    # Method 2: GraphQL UserByScreenName (needs a browser session account)
    if accounts:
        acc = accounts[0]
        bearer = acc.bearer
        csrf   = acc.csrf
        cookie = acc.cookie
        # Known query hash for UserByScreenName (rotates occasionally)
        for qhash in ("qW5u-DAuXpMEG0zA1F7UGQ", "G3KGOASz96M-Ou0nDFvC7A"):
            try:
                variables = json.dumps(
                    {"screen_name": username, "withSafetyModeUserFields": True},
                    separators=(",", ":"),
                )
                features = json.dumps(
                    {"hidden_profile_subscriptions_enabled": True,
                     "rweb_tipjar_consumption_enabled": False,
                     "verified_phone_label_enabled": False,
                     "subscriptions_verification_info_is_identity_verified_enabled": False,
                     "subscriptions_verification_info_verified_since_enabled": True,
                     "highlights_tweets_tab_ui_enabled": True,
                     "responsive_web_twitter_article_notes_tab_enabled": False,
                     "creator_subscriptions_tweet_preview_api_enabled": True,
                     "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                     "responsive_web_graphql_timeline_navigation_enabled": True},
                    separators=(",", ":"),
                )
                from urllib.parse import quote
                url = (
                    f"https://x.com/i/api/graphql/{qhash}/UserByScreenName"
                    f"?variables={quote(variables)}&features={quote(features)}"
                )
                headers = {
                    "authorization": bearer,
                    "x-csrf-token": csrf,
                    "cookie": cookie,
                    "x-twitter-active-user": "yes",
                    "x-twitter-auth-type": "OAuth2Session",
                    "user-agent": ua,
                    "accept": "*/*",
                    "referer": "https://x.com/",
                }
                r = _requests_mod.get(url, headers=headers, timeout=10.0, allow_redirects=False)
                if r.status_code == 200:
                    data = r.json()
                    # Walk to result.data.user.result.rest_id
                    try:
                        uid = data["data"]["user"]["result"]["rest_id"]
                        if str(uid).isdigit():
                            return str(uid)
                    except (KeyError, TypeError):
                        pass
            except Exception:
                pass

    return None


# ── detection recorder ────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, maxlen: int = 20):
        self._lock = threading.Lock()
        self._detections: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self._observations: collections.deque[dict] = collections.deque(maxlen=maxlen * 4)
        self._total = 0

    def record(self, det: dict) -> None:
        with self._lock:
            self._detections.append(det)
            self._total += 1

    def observe(self, det: dict, fired: bool, reason: str = "") -> None:
        row = dict(det)
        row["fired"] = bool(fired)
        row["reason"] = reason
        row["observed_at"] = time.time()
        with self._lock:
            self._observations.append(row)

    def snapshot(self) -> tuple[list[dict], int]:
        with self._lock:
            return list(self._detections), self._total

    def observations(self) -> list[dict]:
        with self._lock:
            return list(self._observations)


class _TweetDeduplicator:
    """
    Thread-safe single-fire deduplicator for multi-source detection.

    All pollers (GQL, REST, SSE, Playwright) route detections through here.
    Each tweet_id fires on_new exactly once regardless of how many sources
    detect it or in what order.

    Timestamp guard: only tweets with snowflake timestamp >= (startup_time - grace_s)
    fire the callback.  This silently drops pre-existing tweets and quoted/embedded
    tweet objects from old content, no matter what GQL returns in its response body.
    """

    def __init__(self, on_new, startup_time: float, grace_s: float = 60.0, on_seen=None):
        self._seen: dict[str, float] = {}   # tid -> first_seen_unix (for checkpoint)
        self._lock = threading.Lock()
        self._on_new = on_new
        self._on_seen = on_seen
        self._min_unix = startup_time - grace_s

    def pre_seed(self, tweet_ids) -> None:
        now = time.time()
        with self._lock:
            for t in tweet_ids:
                if t:
                    self._seen.setdefault(str(t), now)

    def add(self, det: dict) -> bool:
        tid = str(det.get("tweet_id") or "")
        if not tid:
            return False
        snow = det.get("snowflake_unix")
        if snow is not None and snow < self._min_unix:
            return False
        with self._lock:
            if tid in self._seen:
                if self._on_seen:
                    self._on_seen(det, False, "duplicate")
                return False
            self._seen[tid] = time.time()
        if self._on_seen:
            self._on_seen(det, True, "new")
        self._on_new(det)
        return True

    def export_seen(self) -> dict[str, float]:
        with self._lock:
            return dict(self._seen)

    def restore_seen(self, seen_map) -> None:
        if not isinstance(seen_map, dict):
            return
        with self._lock:
            for k, v in seen_map.items():
                if isinstance(v, (int, float)):
                    self._seen[str(k)] = float(v)


# ── display ───────────────────────────────────────────────────────────────────

def _lat_color(ms: Optional[float]) -> str:
    if ms is None:
        return _c("  n/a  ", _DIM)
    if ms < 500:
        return _c(f"{ms:6.0f}ms", _GREEN)
    if ms < 2000:
        return _c(f"{ms:6.0f}ms", _YELLOW)
    return _c(f"{ms:6.0f}ms", _RED)


def _render(
    username: str,
    user_id: str,
    pool_state: dict,
    rotator_status: dict,
    home_rotator_status: dict,
    notifications_rotator_status: dict,
    rest_rotator_status: dict,
    detections: list[dict],
    observations: list[dict],
    total: int,
    gql_count: Optional[int],
    gql_age_s: Optional[float],
    home_count: Optional[int],
    home_age_s: Optional[float],
    notifications_count: Optional[int],
    notifications_age_s: Optional[float],
    started_at: float,
    pw_state: Optional[dict] = None,
    health_snapshot: Optional[dict] = None,
    xtracker_state: Optional[dict] = None,
    hash_status: Optional[dict] = None,
) -> str:
    now = datetime.now(timezone.utc)
    uptime = int(time.time() - started_at)
    lines: list[str] = []

    lines.append(
        _c(f"  polybot watch — @{username}  (uid {user_id})", _BOLD, _WHITE)
        + _c(f"   {now.strftime('%H:%M:%S UTC')}", _DIM)
        + _c(f"   uptime {uptime}s", _DIM)
    )
    lines.append("")

    # SSE health
    alive = pool_state.get("alive", 0)
    total_conns = pool_state.get("total", 0)
    seen_sse = pool_state.get("seen_tweets", 0)
    sse_ok = alive == total_conns and total_conns > 0
    sse_color = _GREEN if sse_ok else (_YELLOW if alive > 0 else _RED)
    lines.append(
        f"  SSE connections   {_c(f'{alive}/{total_conns} alive', sse_color)}"
        f"   tweets seen via SSE: {seen_sse}"
    )

    # Playwright SSE
    if pw_state is not None:
        pw_alive = pw_state.get("alive", False)
        pw_frames = pw_state.get("frames", 0)
        pw_color = _GREEN if pw_alive else _RED
        lines.append(
            f"  Playwright SSE    {_c('alive' if pw_alive else 'offline', pw_color)}"
            f"   frames: {pw_frames}"
        )

    # GQL polling lane
    gql_active  = rotator_status.get("active", 0)
    gql_cooling = rotator_status.get("cooling", 0)
    gql_total   = rotator_status.get("total", 0)
    min_interval = rotator_status.get("min_interval_s", 0)
    aggregate_interval = rotator_status.get("aggregate_interval_s")
    lane_next = rotator_status.get("lane_next_in_s", 0.0)
    rot_color = _GREEN if gql_active == gql_total else (_YELLOW if gql_active > 0 else _RED)
    gql_info = ""
    if gql_count is not None:
        age_str = f"  lifetime_count={gql_count}"
        if gql_age_s is not None:
            age_str += _c(f"  ({gql_age_s:.1f}s ago)", _DIM)
        gql_info = age_str
    lines.append(
        f"  GQL lane          {_c(f'{gql_active} ready, {gql_cooling} waiting / {gql_total} total', rot_color)}"
        + _c(f"   (min_interval={min_interval:.1f}s)", _DIM)
        + (_c(f"  lane={aggregate_interval:.2f}s next={lane_next:.1f}s", _DIM) if isinstance(aggregate_interval, (int, float)) else "")
        + gql_info
    )

    # HomeLatestTimeline polling lane
    home_disabled = home_rotator_status.get("disabled", False)
    if home_disabled:
        home_label = _c("disabled (use --home-timeline to enable)", _DIM)
    else:
        home_active  = home_rotator_status.get("active", 0)
        home_cooling = home_rotator_status.get("cooling", 0)
        home_total   = home_rotator_status.get("total", 0)
        home_min_interval = home_rotator_status.get("min_interval_s", 0)
        home_aggregate_interval = home_rotator_status.get("aggregate_interval_s")
        home_lane_next = home_rotator_status.get("lane_next_in_s", 0.0)
        home_color = _GREEN if home_active == home_total else (_YELLOW if home_active > 0 else _RED)
        home_info = ""
        if home_count is not None:
            home_info = f"  payload_tweets={home_count}"
            if home_age_s is not None:
                home_info += _c(f"  ({home_age_s:.1f}s ago)", _DIM)
        if health_snapshot is not None:
            home_extra = (
                (health_snapshot.get("sources", {}).get("home", {}) or {})
                .get("extra", {})
            )
            latest_snow = home_extra.get("latest_snowflake_unix")
            if isinstance(latest_snow, (int, float)):
                latest_age = max(0.0, time.time() - float(latest_snow))
                home_info += _c(f"  newest_age={latest_age:.1f}s", _DIM)
        home_label = (
            _c(f"{home_active} ready, {home_cooling} waiting / {home_total} total", home_color)
            + _c(f"   (min_interval={home_min_interval:.1f}s)", _DIM)
            + (_c(f"  lane={home_aggregate_interval:.2f}s next={home_lane_next:.1f}s", _DIM) if isinstance(home_aggregate_interval, (int, float)) else "")
            + home_info
        )
    lines.append(f"  Home lane         {home_label}")

    # Notifications polling lane
    notif_disabled = notifications_rotator_status.get("disabled", False)
    if notif_disabled:
        notif_label = _c("disabled (use --notifications to enable)", _DIM)
    else:
        notif_active  = notifications_rotator_status.get("active", 0)
        notif_waiting = notifications_rotator_status.get("cooling", 0)
        notif_total   = notifications_rotator_status.get("total", 0)
        notif_min_interval = notifications_rotator_status.get("min_interval_s", 0)
        notif_aggregate_interval = notifications_rotator_status.get("aggregate_interval_s")
        notif_lane_next = notifications_rotator_status.get("lane_next_in_s", 0.0)
        notif_color = _GREEN if notif_active > 0 else _YELLOW
        notif_info = ""
        if notifications_count is not None:
            notif_info = f"  payload_tweets={notifications_count}"
            if notifications_age_s is not None:
                notif_info += _c(f"  ({notifications_age_s:.1f}s ago)", _DIM)
        if health_snapshot is not None:
            notif_extra = (
                (health_snapshot.get("sources", {}).get("notifications", {}) or {})
                .get("extra", {})
            )
            last_kind = notif_extra.get("last_kind")
            last_status = notif_extra.get("last_status")
            if last_kind:
                notif_info += _c(f"  api={last_kind}", _DIM)
            if last_status:
                notif_info += _c(f" status={last_status}", _DIM)
            body_len = notif_extra.get("body_len")
            username_hits = notif_extra.get("username_hits")
            status_urls = notif_extra.get("status_url_count")
            watched_urls = notif_extra.get("watched_status_url_count")
            if isinstance(body_len, int):
                notif_info += _c(f" bytes={body_len}", _DIM)
            if isinstance(status_urls, int):
                notif_info += _c(f" urls={status_urls}", _DIM)
            if isinstance(watched_urls, int):
                notif_info += _c(f" watched_urls={watched_urls}", _DIM)
            if isinstance(username_hits, int):
                notif_info += _c(f" user_hits={username_hits}", _DIM)
        notif_label = (
            _c(f"{notif_active} ready, {notif_waiting} waiting / {notif_total} total", notif_color)
            + _c(f"   (min_interval={notif_min_interval:.1f}s)", _DIM)
            + (_c(f"  lane={notif_aggregate_interval:.2f}s next={notif_lane_next:.1f}s", _DIM) if isinstance(notif_aggregate_interval, (int, float)) else "")
            + notif_info
        )
    lines.append(f"  Notifications     {notif_label}")

    # Per-account budget detail (GQL)
    budget = rotator_status.get("budget") or {}
    if budget:
        lines.append(_c("    GQL account pacing", _DIM))
        budget_lines = []
        for label, b in budget.items():
            rem = b.get("remaining")
            next_in = b.get("next_poll_in_s", 0.0)
            pace = b.get("budget_pace_s")
            budget_wait = b.get("budget_wait_s") or 0.0
            min_wait = b.get("min_wait_s") or 0.0
            lane_wait = b.get("lane_wait_s") or 0.0
            stat = b.get("status", "?")
            if rem is None:
                rem_str = _c("rem=?", _DIM)
            elif rem >= 50:
                rem_str = _c(f"rem={rem}", _GREEN)
            elif rem >= 10:
                rem_str = _c(f"rem={rem}", _YELLOW)
            else:
                rem_str = _c(f"rem={rem}", _RED)
            ready_str = _c("READY", _GREEN) if next_in <= 0.1 else _c(f"in {next_in:.1f}s", _DIM)
            stat_short = stat.split(" (")[0] if " (" in stat else stat
            if stat_short == "ok":
                stat_col = _c("ok", _GREEN)
            elif stat_short == "429":
                stat_col = _c("429", _RED)
            elif stat_short.startswith("err"):
                stat_col = _c(stat_short, _RED)
            else:
                stat_col = _c(stat_short, _YELLOW)
            pace_str = _c(f"pace={pace:.1f}s", _DIM) if isinstance(pace, (int, float)) else _c("pace=?", _DIM)
            wait_src = "lane" if lane_wait >= max(budget_wait, min_wait) and lane_wait > 0.1 else ("budget" if budget_wait >= min_wait and budget_wait > 0.1 else "min")
            budget_lines.append(f"    {label:<20} {stat_col:<30}  {rem_str:<14}  {pace_str:<16}  next: {ready_str} ({wait_src})")
        for bl in budget_lines:
            lines.append(bl)

    # Per-account budget detail (HomeLatestTimeline)
    home_budget = home_rotator_status.get("budget") or {}
    if home_budget and not home_disabled:
        lines.append(_c("    Home account pacing", _DIM))
        for label, b in home_budget.items():
            rem = b.get("remaining")
            next_in = b.get("next_poll_in_s", 0.0)
            pace = b.get("budget_pace_s")
            budget_wait = b.get("budget_wait_s") or 0.0
            min_wait = b.get("min_wait_s") or 0.0
            lane_wait = b.get("lane_wait_s") or 0.0
            stat = b.get("status", "?")
            if rem is None:
                rem_str = _c("rem=?", _DIM)
            elif rem >= 100:
                rem_str = _c(f"rem={rem}", _GREEN)
            elif rem >= 20:
                rem_str = _c(f"rem={rem}", _YELLOW)
            else:
                rem_str = _c(f"rem={rem}", _RED)
            ready_str = _c("READY", _GREEN) if next_in <= 0.1 else _c(f"in {next_in:.1f}s", _DIM)
            stat_short = stat.split(" (")[0] if " (" in stat else stat
            stat_col = _c("ok", _GREEN) if stat_short == "ok" else _c(stat_short, _YELLOW)
            pace_str = _c(f"pace={pace:.1f}s", _DIM) if isinstance(pace, (int, float)) else _c("pace=?", _DIM)
            wait_src = "lane" if lane_wait >= max(budget_wait, min_wait) and lane_wait > 0.1 else ("budget" if budget_wait >= min_wait and budget_wait > 0.1 else "min")
            lines.append(f"    {label:<20} {stat_col:<30}  {rem_str:<14}  {pace_str:<16}  next: {ready_str} ({wait_src})")

    # REST polling lane
    rest_active  = rest_rotator_status.get("active", 0)
    rest_cooling = rest_rotator_status.get("cooling", 0)
    rest_total   = rest_rotator_status.get("total", 0)
    rest_disabled = rest_rotator_status.get("disabled", False)
    if rest_disabled:
        rest_label = _c("disabled (use --rest to enable; usually returns 401)", _DIM)
    else:
        rest_color = _GREEN if rest_active == rest_total else (_YELLOW if rest_active > 0 else _RED)
        rest_label = _c(f"{rest_active} active, {rest_cooling} cooling / {rest_total} total", rest_color)
    lines.append(f"  REST lane         {rest_label}")

    # ── Reliability section (only when health_snapshot is provided) ──────
    if health_snapshot is not None:
        lines.append("")
        lines.append(_c("  Reliability", _BOLD))
        # XTracker line
        if xtracker_state is not None:
            xt_count = xtracker_state.get("last_count")
            xt_age = xtracker_state.get("last_count_age_s")
            div = xtracker_state.get("divergence_since")
            xt_color = _GREEN if xt_age is not None and xt_age < 90 else (_YELLOW if xt_age is not None and xt_age < 300 else _RED)
            line = f"  XTracker          {_c(f'count={xt_count}', xt_color)}"
            if xt_age is not None:
                line += _c(f"  (age {xt_age:.0f}s)", _DIM)
            if div is not None:
                line += "  " + _c("⚠ DIVERGING", _RED)
            lines.append(line)
        # Hash status
        if hash_status is not None:
            hashes = hash_status.get("hashes", {})
            ut = hashes.get("UserTweets", "?")
            ht = hashes.get("HomeLatestTimeline", "?")
            refresh_ok = hash_status.get("last_refresh_ok", False)
            changes = hash_status.get("change_count", 0)
            col = _GREEN if refresh_ok else _RED
            lines.append(
                f"  GQL hashes        {_c('UserTweets=' + (ut[:18] + '...' if len(ut)>18 else ut), col)}"
                + "  "
                + _c('Home=' + (ht[:18] + '...' if len(ht)>18 else ht), col)
                + _c(f"  refreshes={hash_status.get('refresh_count',0)} changes={changes}", _DIM)
            )
        # No-signal warning
        last_det_age = health_snapshot.get("last_global_detection_age_s")
        if last_det_age is None:
            sig_str = _c("no detection yet", _YELLOW)
        elif last_det_age < 300:
            sig_str = _c(f"{last_det_age:.0f}s ago", _GREEN)
        elif last_det_age < 600:
            sig_str = _c(f"{last_det_age:.0f}s ago", _YELLOW)
        else:
            sig_str = _c(f"{last_det_age:.0f}s ago !!", _RED)
        lines.append(f"  Last detection    {sig_str}")
        # Recent alerts
        recent = health_snapshot.get("recent_alerts", [])
        if recent:
            for a in recent[-3:]:
                level = a.get("level", "info")
                col = {"info": _CYAN, "warn": _YELLOW, "critical": _RED}.get(level, _DIM)
                lines.append(
                    f"  alert  {_c(level.upper(), col)}  {a.get('code')}: {a.get('message','')[:80]}"
                )
        # Restart count
        restarts = health_snapshot.get("restart_count", {})
        if restarts:
            lines.append(
                "  Restarts:         "
                + ", ".join(f"{k}={v}" for k, v in restarts.items())
            )

    lines.append("")
    lines.append(f"  {_c('Recent detections', _BOLD)}   (total this session: {total})")
    lines.append(f"  {'Time':>8}  {'Tweet ID':>19}  {'Latency':>8}  {'Method':<22}  Text")
    lines.append("  " + "─" * 90)

    if not detections:
        # Honest hint depending on context. The pipeline is provably healthy
        # if GQL is fetching counts — no need to wait for a tweet to confirm it.
        gql_alive = (
            health_snapshot is not None
            and (health_snapshot.get("sources", {}).get("gql", {}) or {}).get("alive")
        )
        if gql_alive:
            lines.append(_c(
                f"  No tweets yet — pipeline IS healthy (GQL polling @{username} fine). "
                f"Waiting for next tweet.", _DIM))
            lines.append(_c(
                "  To self-test, run a second watch on an account you own: "
                "watch.bat --username <your_handle>", _DIM))
        else:
            lines.append(_c(
                f"  No tweets detected yet — tweet from @{username} (or watch a "
                f"different account) to test!", _DIM))
    else:
        for det in reversed(list(detections)[-15:]):
            ts = det.get("detected_at")
            ts_str = (
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
                if isinstance(ts, (int, float)) else "  ?"
            )
            tid = str(det.get("tweet_id") or "?")
            lat = det.get("latency_ms")
            method = str(det.get("method") or "?")[:22]
            text = str(det.get("text") or "")[:50].replace("\n", " ")
            lines.append(
                f"  {ts_str:>8}  {tid:>19}  {_lat_color(lat):>8}  {method:<22}  {_c(text, _DIM)}"
            )

    if observations:
        lines.append("")
        lines.append(f"  {_c('Source race', _BOLD)}   (new = fired, dup = saw after another source)")
        lines.append(f"  {'Time':>8}  {'Tweet ID':>19}  {'Latency':>8}  {'Method':<22}  State")
        lines.append("  " + "─" * 74)
        for obs in reversed(list(observations)[-12:]):
            ts = obs.get("observed_at") or obs.get("detected_at")
            ts_str = (
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
                if isinstance(ts, (int, float)) else "  ?"
            )
            tid = str(obs.get("tweet_id") or "?")
            lat = obs.get("latency_ms")
            method = str(obs.get("method") or "?")[:22]
            state = "new" if obs.get("fired") else str(obs.get("reason") or "seen")
            col = _GREEN if state == "new" else _DIM
            lines.append(
                f"  {ts_str:>8}  {tid:>19}  {_lat_color(lat):>8}  {method:<22}  {_c(state, col)}"
            )

    lines.append("")
    lines.append(_c("  Press Ctrl+C to stop", _DIM))
    return "\n".join(lines)


# ── polling helpers ────────────────────────────────────────────────────────────

_GQL_HASH = "36rb3Xj3iJ64Q-9wKDjCcQ"
_HOME_LATEST_HASH = "SFxmNKWfN9ySJcXG_tjX8g"
_POLL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_HOME_LATEST_FEATURES = {
    "rweb_video_screen_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
}
_HOME_LATEST_FIELD_TOGGLES = {"withArticlePlainText": False}


def _make_sessions(accounts: list) -> dict:
    sessions = {}
    for acc in accounts:
        s = _requests_mod.Session()
        s.headers.update({
            "authorization": acc.bearer,
            "x-csrf-token": acc.csrf,
            "cookie": acc.cookie,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "accept": "*/*",
            "origin": "https://x.com",
            "referer": "https://x.com/",
            "user-agent": _POLL_UA,
        })
        sessions[acc.label] = s
    return sessions


def _walk_gql_tweets(obj, watch_user_id: str, out: dict) -> None:
    """Walk GQL response, collect {tweet_id: text} for original tweets by watch_user_id."""
    if isinstance(obj, dict):
        legacy = obj.get("legacy")
        if isinstance(legacy, dict):
            tid = legacy.get("id_str")
            if isinstance(tid, str) and tid.isdigit():
                # Check authorship — filter out quoted tweets from other users
                author = legacy.get("user_id_str") or obj.get("user_id_str")
                if not author:
                    core = obj.get("core")
                    if isinstance(core, dict):
                        ur = core.get("user_results")
                        if isinstance(ur, dict):
                            r2 = ur.get("result")
                            if isinstance(r2, dict):
                                author = r2.get("rest_id")
                if str(author) == watch_user_id:
                    if (not legacy.get("in_reply_to_status_id_str")
                            and not legacy.get("retweeted_status_id_str")
                            and "retweeted_status_result" not in obj):
                        out[tid] = legacy.get("full_text", "")
        for v in obj.values():
            _walk_gql_tweets(v, watch_user_id, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_gql_tweets(v, watch_user_id, out)


def _walk_notification_tweets(obj, watch_username: str, watch_user_id: str, out: dict) -> None:
    """
    Extract watched-user tweets from notification payloads.

    Notifications responses are less consistent than Tweet timelines: sometimes
    they contain full Tweet objects, sometimes just entities/URLs pointing at
    /<username>/status/<tweet_id>. Use both paths.
    """
    _walk_gql_tweets(obj, watch_user_id, out)
    username = str(watch_username or "").lower().lstrip("@")
    if not username:
        return
    url_re = re.compile(
        rf"(?:https?://(?:x|twitter)\.com/)?{re.escape(username)}/status/(\d+)",
        re.IGNORECASE,
    )

    def visit(v, text_hint: str = "") -> None:
        if isinstance(v, dict):
            next_text = text_hint
            legacy = v.get("legacy")
            if isinstance(legacy, dict):
                next_text = (
                    legacy.get("full_text")
                    or legacy.get("text")
                    or next_text
                )
            for key in ("full_text", "text", "title", "description"):
                if isinstance(v.get(key), str) and v.get(key):
                    next_text = v.get(key)
            for val in v.values():
                visit(val, next_text)
        elif isinstance(v, list):
            for item in v:
                visit(item, text_hint)
        elif isinstance(v, str):
            for m in url_re.finditer(v):
                tid = m.group(1)
                if tid.isdigit():
                    out.setdefault(tid, text_hint or "")

    visit(obj)


def _summarize_notification_payload(obj, watch_username: str) -> dict:
    """Small debug summary for NotificationsTimeline payloads."""
    username = str(watch_username or "").lower().lstrip("@")
    status_re = re.compile(
        r"(?:https?://(?:x|twitter)\.com/)?([A-Za-z0-9_]{1,20})/status/(\d+)",
        re.IGNORECASE,
    )
    status_ids: list[tuple[str, str]] = []
    username_hits = 0
    strings_seen = 0

    def visit(v) -> None:
        nonlocal username_hits, strings_seen
        if isinstance(v, dict):
            for val in v.values():
                visit(val)
        elif isinstance(v, list):
            for item in v:
                visit(item)
        elif isinstance(v, str):
            strings_seen += 1
            low = v.lower()
            if username and username in low:
                username_hits += 1
            for m in status_re.finditer(v):
                status_ids.append((m.group(1), m.group(2)))

    visit(obj)
    watched_status_ids = [
        tid for user, tid in status_ids
        if username and user.lower() == username
    ]
    return {
        "strings_seen": strings_seen,
        "username_hits": username_hits,
        "status_url_count": len(status_ids),
        "watched_status_url_count": len(watched_status_ids),
        "status_url_samples": [
            f"{user}/{tid}" for user, tid in status_ids[:5]
        ],
        "watched_status_url_samples": watched_status_ids[:5],
    }


def _walk_statuses(obj) -> Optional[int]:
    if isinstance(obj, dict):
        if "statuses_count" in obj:
            try:
                return int(obj["statuses_count"])
            except Exception:
                return None
        for v in obj.values():
            r = _walk_statuses(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_statuses(v)
            if r is not None:
                return r
    return None


def _preseed(user_id: str, accounts: list, dedup: "_TweetDeduplicator") -> None:
    """
    Fetch the last 20 tweets before pollers start so we don't re-report existing tweets.
    Uses the first available account; silently skips on failure.
    """
    from urllib.parse import quote as _q
    import json as _j
    if not accounts:
        return
    acc = accounts[0]
    variables = _j.dumps({
        "userId": user_id, "count": 20,
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": False,
        "withVoice": False, "withV2Timeline": True,
    })
    url = (
        f"https://x.com/i/api/graphql/{_GQL_HASH}/UserTweets"
        f"?variables={_q(variables)}"
    )
    try:
        r = _requests_mod.get(
            url,
            headers={
                "authorization": acc.bearer,
                "x-csrf-token": acc.csrf,
                "cookie": acc.cookie,
                "x-twitter-active-user": "yes",
                "x-twitter-auth-type": "OAuth2Session",
                "accept": "*/*",
                "user-agent": _POLL_UA,
            },
            timeout=10.0,
            allow_redirects=False,
        )
        if r.status_code == 200:
            tweets: dict = {}
            _walk_gql_tweets(r.json(), user_id, tweets)
            dedup.pre_seed(tweets.keys())
    except Exception:
        pass


# ── GQL poller thread ─────────────────────────────────────────────────────────

def _gql_poll_loop(
    user_id: str,
    accounts: list,
    rotator: AccountRotator,
    on_count,
    stop_evt: threading.Event,
    dedup: "_TweetDeduplicator",
    health=None,            # optional HealthState
    hash_registry=None,     # optional HashRegistry
) -> None:
    """
    Single shared poll thread.

    When `hash_registry` is provided, the URL's queryId is read fresh on
    every iteration so a mid-run hash rotation is picked up automatically.
    On 404, the registry is notified to refresh immediately.

    When `health` is provided, source "gql" is heartbeat'd and 200/error
    outcomes are reported so the watchdog and divergence detector see them.
    """
    from urllib.parse import quote as _q
    import json as _j

    clients = _make_sessions(accounts)

    variables = _j.dumps({
        "userId": user_id, "count": 5,
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": False,
        "withVoice": False, "withV2Timeline": True,
    })
    variables_q = _q(variables)

    if health is not None:
        health.register("gql")

    _hb = (lambda: health.heartbeat("gql")) if health is not None else None
    while not stop_evt.is_set():
        if _hb is not None:
            _hb()

        acc = rotator.wait_and_get(stop_evt, heartbeat=_hb)
        if acc is None:
            break
        client = clients.get(acc.label)
        if client is None:
            continue

        # Build URL with the current hash (re-read every iteration for hot-swap)
        gql_hash = hash_registry.get("UserTweets") if hash_registry else None
        if not gql_hash:
            gql_hash = _GQL_HASH
        url = (
            f"https://x.com/i/api/graphql/{gql_hash}/UserTweets"
            f"?variables={variables_q}"
        )

        per_acc_src = f"gql:{acc.label}"
        try:
            r = client.get(url, timeout=8.0, allow_redirects=False)
            rotator.mark_response(acc.label, r.status_code, r.headers)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    if health is not None:
                        health.mark_error("gql", "bad_json")
                    continue
                count = _walk_statuses(data)
                if count is not None:
                    on_count(count)
                tweets: dict = {}
                _walk_gql_tweets(data, user_id, tweets)
                now = time.time()
                if health is not None:
                    health.mark_success(
                        "gql",
                        lifetime_count=count,
                        extra={"last_account": acc.label, "hash": gql_hash},
                    )
                for tid, text in tweets.items():
                    snow = snowflake_unix(tid)
                    lat_ms = round((now - snow) * 1000.0, 1) if snow else None
                    det = {
                        "tweet_id": tid,
                        "method": f"gql/{acc.label}",
                        "detected_at": now,
                        "snowflake_unix": snow,
                        "latency_ms": lat_ms,
                        "text": (text or "")[:120],
                    }
                    if dedup.add(det) and health is not None:
                        health.mark_success("gql", detection=True)
            elif r.status_code == 404:
                # Likely hash rotated — trigger immediate refresh
                if hash_registry is not None:
                    hash_registry.notify_404("UserTweets")
                if health is not None:
                    health.mark_error("gql", "404")
            elif r.status_code == 401:
                if health is not None:
                    health.mark_401(per_acc_src)
                    health.mark_error("gql", "401")
            elif r.status_code == 429:
                if health is not None:
                    health.mark_error("gql", "429")
            else:
                if health is not None:
                    health.mark_error("gql", f"http_{r.status_code}")
        except Exception as e:
            rotator.mark_error(acc.label)
            if health is not None:
                health.mark_error("gql", f"exc:{e}")


# ── HomeLatestTimeline poller thread ───────────────────────────────────────────

def _home_timeline_poll_loop(
    user_id: str,
    accounts: list,
    rotator: AccountRotator,
    on_count,
    stop_evt: threading.Event,
    dedup: "_TweetDeduplicator",
    request_context: str = "ptr",
    health=None,            # optional HealthState
    hash_registry=None,     # optional HashRegistry
) -> None:
    """
    Poll authenticated reverse-chronological home timelines.

    This lane is useful when the polling accounts follow the watched user:
    X may surface the fresh tweet in HomeLatestTimeline before UserTweets
    catches up. It uses a separate rotator so HomeLatestTimeline rate-limit
    headers are tracked independently from the UserTweets lane.
    """
    from urllib.parse import quote as _q
    import json as _j

    clients = _make_sessions(accounts)

    variables = _j.dumps({
        "count": 20,
        "includePromotedContent": False,
        "latestControlAvailable": True,
        "requestContext": request_context,
    }, separators=(",", ":"))
    features = _j.dumps(_HOME_LATEST_FEATURES, separators=(",", ":"))
    field_toggles = _j.dumps(_HOME_LATEST_FIELD_TOGGLES, separators=(",", ":"))
    query_q = (
        f"variables={_q(variables)}"
        f"&features={_q(features)}"
        f"&fieldToggles={_q(field_toggles)}"
    )

    if health is not None:
        health.register("home")

    _hb = (lambda: health.heartbeat("home")) if health is not None else None
    while not stop_evt.is_set():
        if _hb is not None:
            _hb()

        acc = rotator.wait_and_get(stop_evt, heartbeat=_hb)
        if acc is None:
            break
        client = clients.get(acc.label)
        if client is None:
            continue

        home_hash = hash_registry.get("HomeLatestTimeline") if hash_registry else None
        if not home_hash:
            home_hash = _HOME_LATEST_HASH
        url = (
            f"https://x.com/i/api/graphql/{home_hash}/HomeLatestTimeline"
            f"?{query_q}"
        )

        per_acc_src = f"home:{acc.label}"
        try:
            r = client.get(url, timeout=8.0, allow_redirects=False)
            rotator.mark_response(acc.label, r.status_code, r.headers)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    if health is not None:
                        health.mark_error("home", "bad_json")
                    continue

                tweets: dict = {}
                _walk_gql_tweets(data, user_id, tweets)
                on_count(len(tweets))
                now = time.time()
                latest_tid = max(tweets.keys(), key=int) if tweets else ""
                latest_snow = snowflake_unix(latest_tid) if latest_tid else None
                if health is not None:
                    health.mark_success(
                        "home",
                        extra={
                            "last_account": acc.label,
                            "hash": home_hash,
                            "request_context": request_context,
                            "payload_tweets": len(tweets),
                            "latest_tweet_id": latest_tid,
                            "latest_snowflake_unix": latest_snow,
                            "latest_text": (tweets.get(latest_tid) or "")[:80] if latest_tid else "",
                        },
                    )
                for tid, text in tweets.items():
                    snow = snowflake_unix(tid)
                    lat_ms = round((now - snow) * 1000.0, 1) if snow else None
                    det = {
                        "tweet_id": tid,
                        "method": f"home/{acc.label}",
                        "detected_at": now,
                        "snowflake_unix": snow,
                        "latency_ms": lat_ms,
                        "text": (text or "")[:120],
                    }
                    if dedup.add(det) and health is not None:
                        health.mark_success("home", detection=True)
            elif r.status_code == 404:
                if hash_registry is not None:
                    hash_registry.notify_404("HomeLatestTimeline")
                if health is not None:
                    health.mark_error("home", "404")
            elif r.status_code == 401:
                if health is not None:
                    health.mark_401(per_acc_src)
                    health.mark_error("home", "401")
            elif r.status_code == 429:
                if health is not None:
                    health.mark_error("home", "429")
            else:
                if health is not None:
                    health.mark_error("home", f"http_{r.status_code}")
        except Exception as e:
            rotator.mark_error(acc.label)
            if health is not None:
                health.mark_error("home", f"exc:{e}")


# ── REST poller thread (independent rate-limit pool) ──────────────────────────

def _notifications_poll_loop(
    username: str,
    user_id: str,
    accounts: list,
    rotator: AccountRotator,
    on_count,
    stop_evt: threading.Event,
    dedup: "_TweetDeduplicator",
    health=None,            # optional HealthState
) -> None:
    """
    Poll X's real Notifications page/API via Playwright.

    This intentionally does not guess a GraphQL operation name. It opens
    /notifications as the logged-in account, waits for the browser's own
    NotificationsTimeline/Notification API response, and scans that JSON for
    original tweets by the watched user.
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
        from signals.sse_playwright import _parse_cookies_for_playwright
    except Exception as e:
        if health is not None:
            health.register("notifications")
            health.mark_error("notifications", f"playwright_import:{e}")
        return

    if health is not None:
        health.register("notifications")

    pages = {}
    first_load: dict[str, bool] = {}
    clients_ready = False

    def _is_notifications_response(resp) -> bool:
        url = resp.url or ""
        return (
            "NotificationsTimeline" in url
            or ("/i/api/graphql/" in url and "Notification" in url)
            or ("/i/api/2/notifications/" in url)
        )

    def _notification_kind(url: str) -> str:
        if "NotificationsTimeline" in url:
            return "NotificationsTimeline"
        if "/i/api/2/notifications/" in url:
            return "api2_notifications"
        if "/i/api/graphql/" in url and "Notification" in url:
            return "graphql_notification"
        return "unknown"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for acc in accounts:
                if stop_evt.is_set():
                    break
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    )
                )
                ctx.add_cookies(_parse_cookies_for_playwright(acc.cookie))
                page = ctx.new_page()
                pages[acc.label] = page
                first_load[acc.label] = True
            clients_ready = bool(pages)

            if not clients_ready:
                if health is not None:
                    health.mark_error("notifications", "no_pages")
                return

            while not stop_evt.is_set():
                if health is not None:
                    health.heartbeat("notifications")
                acc = rotator.wait_and_get(
                    stop_evt,
                    heartbeat=(lambda: health.heartbeat("notifications")) if health is not None else None,
                )
                if acc is None:
                    break
                page = pages.get(acc.label)
                if page is None:
                    continue

                per_acc_src = f"notifications:{acc.label}"
                try:
                    with page.expect_response(_is_notifications_response, timeout=15000) as resp_info:
                        if first_load.get(acc.label, True):
                            page.goto("https://x.com/notifications", wait_until="domcontentloaded", timeout=30000)
                            first_load[acc.label] = False
                        else:
                            page.reload(wait_until="domcontentloaded", timeout=30000)

                    resp = resp_info.value
                    rotator.mark_response(acc.label, resp.status, resp.headers)
                    if resp.status != 200:
                        if resp.status == 401 and health is not None:
                            health.mark_401(per_acc_src)
                        if health is not None:
                            health.mark_error("notifications", f"http_{resp.status}")
                        continue
                    try:
                        data = json.loads(resp.text())
                    except Exception:
                        if health is not None:
                            health.mark_error("notifications", "bad_json")
                        continue

                    tweets: dict = {}
                    _walk_notification_tweets(data, username, user_id, tweets)
                    notif_summary = _summarize_notification_payload(data, username)
                    on_count(len(tweets))
                    now = time.time()
                    if health is not None:
                        health.mark_success(
                            "notifications",
                            extra={
                                "last_account": acc.label,
                                "payload_tweets": len(tweets),
                                "last_status": resp.status,
                                "last_kind": _notification_kind(resp.url or ""),
                                "last_url_tail": (resp.url or "")[-120:],
                                "body_len": len(resp.text() or ""),
                                **notif_summary,
                            },
                        )
                    for tid, text in tweets.items():
                        snow = snowflake_unix(tid)
                        lat_ms = round((now - snow) * 1000.0, 1) if snow else None
                        det = {
                            "tweet_id": tid,
                            "method": f"notif/{acc.label}",
                            "detected_at": now,
                            "snowflake_unix": snow,
                            "latency_ms": lat_ms,
                            "text": (text or "")[:120],
                        }
                        if dedup.add(det) and health is not None:
                            health.mark_success("notifications", detection=True)
                except PlaywrightTimeoutError:
                    rotator.mark_error(acc.label)
                    if health is not None:
                        health.mark_error("notifications", "timeout")
                except Exception as e:
                    rotator.mark_error(acc.label)
                    if health is not None:
                        health.mark_error("notifications", f"exc:{e}")
    finally:
        try:
            for page in pages.values():
                page.context.close()
        except Exception:
            pass


def _rest_poll_loop(
    user_id: str,
    accounts: list,
    rotator: AccountRotator,
    stop_evt: threading.Event,
    dedup: "_TweetDeduplicator",
    rest_status: dict,
) -> None:
    """
    Opt-in REST 1.1 statuses/user_timeline polling.
    Browser-session auth usually returns 401 here, so this lane disables
    itself on the first 401. Uses its OWN rotator (separate from GQL) so
    the rate-limit budget is independent per endpoint.
    """
    clients = _make_sessions(accounts)
    url = (
        f"https://x.com/i/api/1.1/statuses/user_timeline.json"
        f"?user_id={user_id}&count=5&exclude_replies=1&include_rts=0&tweet_mode=extended"
    )

    while not stop_evt.is_set():
        if rest_status.get("disabled"):
            break
        acc = rotator.wait_and_get(stop_evt)  # rest lane has no health source registered
        if acc is None:
            break
        client = clients.get(acc.label)
        if client is None:
            continue
        try:
            r = client.get(url, timeout=8.0, allow_redirects=False)
            if r.status_code == 401:
                rest_status["disabled"] = True
                break
            rotator.mark_response(acc.label, r.status_code, r.headers)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    continue
                if not isinstance(data, list):
                    continue
                now = time.time()
                for t in data:
                    if not isinstance(t, dict):
                        continue
                    tid = str(t.get("id_str") or "")
                    if not tid.isdigit():
                        continue
                    text = t.get("full_text") or t.get("text") or ""
                    snow = snowflake_unix(tid)
                    lat_ms = round((now - snow) * 1000.0, 1) if snow else None
                    dedup.add({
                        "tweet_id": tid,
                        "method": f"rest/{acc.label}",
                        "detected_at": now,
                        "snowflake_unix": snow,
                        "latency_ms": lat_ms,
                        "text": (text or "")[:120],
                    })
        except Exception:
            rotator.mark_error(acc.label)


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="watch",
        description="Live terminal monitor for tweet detection (SSE + GQL).",
    )
    p.add_argument("--username", default="elonmusk",
                   help="Twitter @username to watch (default: elonmusk)")
    p.add_argument("--user-id", dest="user_id", default=None,
                   help="Numeric user_id (skip lookup).")
    p.add_argument("--min-interval-s", type=float, default=6.5,
                   help="Per-account minimum interval between polls (seconds). "
                        "Default 6.5s = ~9.2 req/min/account, safely under Twitter's "
                        "~10 req/min/account GQL UserTweets budget. Lower this if you "
                        "have many accounts.")
    p.add_argument("--jitter-s", type=float, default=1.5,
                   help="Random jitter added on top of min-interval-s per account, per pick. "
                        "Default 1.5s gives a 6.5-8.0s gap per account, breaking the periodicity "
                        "that bot detectors look for. 0 disables.")
    p.add_argument("--poll-spread-s", type=float, default=None,
                   help="Initial spacing between accounts in each rotator. Default: spread "
                        "accounts evenly across that lane's min interval. Use 0 to disable.")
    p.add_argument("--home-min-interval-s", type=float, default=2.0,
                   help="Per-account minimum interval for HomeLatestTimeline polling. "
                        "Default 2.0s, based on the larger observed Home timeline budget.")
    p.add_argument("--home-request-context", default="ptr",
                   choices=("ptr", "launch"),
                   help="HomeLatestTimeline requestContext. Default ptr uses the refresh "
                        "path; launch behaves like an initial page load and can be staler.")
    p.add_argument("--no-gql", action="store_true", help="Disable GQL polling.")
    p.add_argument("--sse", action="store_true",
                   help="Enable Twitter live_pipeline SSE connections. "
                        "DEFAULT IS OFF — Twitter's SSE does NOT push new tweets; "
                        "it only pushes engagement events. Including it just adds "
                        "fingerprint surface. Only enable for debugging.")
    p.add_argument("--no-sse", action="store_true",
                   help="Deprecated; SSE is off by default. Kept for backward compat.")
    p.add_argument("--rest", action="store_true",
                   help="Also poll REST 1.1 statuses/user_timeline (separate rate-limit "
                        "bucket from GQL). Disables itself on 401.")
    p.add_argument("--home-timeline", action="store_true",
                   help="Also poll HomeLatestTimeline from each account. Accounts must "
                        "follow the watched user for this to help. Diagnostic only; "
                        "it has proven slower than UserTweets in live tests.")
    p.add_argument("--notifications", action="store_true",
                   help="Poll the logged-in /notifications feed via Playwright. "
                        "Accounts must follow the watched user and have notifications enabled.")
    p.add_argument("--notifications-min-interval-s", type=float, default=1.0,
                   help="Per-account minimum interval for notifications polling.")
    p.add_argument("--playwright", action="store_true",
                   help="Also run Playwright SSE interception (uses first account, needs playwright installed).")
    p.add_argument("--debug", action="store_true", help="Enable DEBUG logging to sse_debug.log.")
    # ── reliability infrastructure ────────────────────────────────────────
    p.add_argument("--no-xtracker", action="store_true",
                   help="Disable XTracker backup poller (default: enabled).")
    p.add_argument("--xtracker-username", default="elonmusk",
                   help="Which username XTracker tracks (default: elonmusk). "
                        "Auto-disabled if it does not match --username.")
    p.add_argument("--no-hash-refresh", action="store_true",
                   help="Disable automatic GQL hash refresh from main.js bundle.")
    p.add_argument("--no-checkpoint", action="store_true",
                   help="Disable disk checkpoint of seen tweet ids.")
    p.add_argument("--no-alerts", action="store_true",
                   help="Disable health.json + Discord/Telegram webhooks.")
    p.add_argument("--no-watchdog", action="store_true",
                   help="Disable thread watchdog (auto-restart of dead pollers).")
    p.add_argument("--health-file", default="data/health.json",
                   help="Path to write health snapshot (default data/health.json).")
    p.add_argument("--checkpoint-file", default="data/watch_state.json",
                   help="Path to persist seen-tweet checkpoint.")
    p.add_argument("--xtracker-interval-s", type=float, default=30.0,
                   help="Interval between XTracker polls.")
    p.add_argument("--alert-no-signal-s", type=float, default=600.0,
                   help="Alert if no tweet detected by ANY source for this many seconds.")
    p.add_argument("--lockfile", default="data/watch.lock",
                   help="PID lockfile path. Prevents multiple instances.")
    args = p.parse_args(argv)

    import logging as _logging
    if args.debug:
        fh = _logging.FileHandler("sse_debug.log", encoding="utf-8")
        fh.setLevel(_logging.DEBUG)
        for _n in ("sse_direct", "sse_pool", "sse_playwright"):
            lg = _logging.getLogger(_n)
            lg.setLevel(_logging.DEBUG)
            lg.addHandler(fh)
        print("[debug] SSE logging -> sse_debug.log")
    else:
        # SSE subscription (user_timeline) always fails with code 32 — suppress the noise.
        # Real errors (connection drops) are tracked in pool.state(), not needed in terminal.
        for _n in ("sse_direct", "sse_pool", "sse_playwright"):
            lg = _logging.getLogger(_n)
            lg.setLevel(_logging.CRITICAL)
            lg.propagate = False

    # ── PID lockfile ──────────────────────────────────────────────────────
    lockfile = args.lockfile
    if lockfile:
        try:
            os.makedirs(os.path.dirname(lockfile) or ".", exist_ok=True)
            if os.path.exists(lockfile):
                try:
                    with open(lockfile) as _lf:
                        old_pid = int(_lf.read().strip())
                    # On Windows, just warn — don't kill. On VPS systemd handles it.
                    print(f"[warn] lockfile {lockfile} exists with pid={old_pid}; "
                          f"continuing anyway (stale lock or another instance running)",
                          file=sys.stderr)
                except Exception:
                    pass
            with open(lockfile, "w") as _lf:
                _lf.write(str(os.getpid()))
        except Exception as e:
            print(f"[warn] could not write lockfile {lockfile}: {e}", file=sys.stderr)

    accounts = load_accounts()
    if not accounts:
        print("No accounts found. Add credentials to accounts.json or .env — see accounts.json.example.", file=sys.stderr)
        return 1

    # Resolve user_id
    user_id = args.user_id
    if user_id is None:
        print(f"Looking up user_id for @{args.username}...", end=" ", flush=True)
        user_id = lookup_user_id(args.username, accounts)
        if user_id:
            print(f"found: {user_id}")
        else:
            print(f"\nCould not resolve user_id for @{args.username}.")
            print("Try passing --user-id <numeric_id> directly.")
            print("You can find it at: https://tweeterid.com")
            return 1

    # ── core state ────────────────────────────────────────────────────────
    from signals.health_state import HealthState
    from signals.alerts import AlertSink, HealthMonitor
    from signals.hash_refresh import HashRegistry
    from signals.xtracker_poll import XTrackerPoller
    from signals.checkpoint import Checkpoint
    from signals.watchdog import ThreadWatchdog

    health = HealthState()
    recorder = Recorder(maxlen=20)
    startup_time = time.time()
    dedup = _TweetDeduplicator(
        recorder.record,
        startup_time=startup_time,
        on_seen=recorder.observe,
    )
    stop_evt = threading.Event()
    rest_status: dict = {}

    def on_account_dead(label: str, reason: str) -> None:
        health.alert(
            level="critical",
            code=f"account_dead_{label}",
            message=f"Account {label} marked dead: {reason}. "
                    f"Refresh cookie/auth or replace it in accounts.json.",
            source=f"gql:{label}",
        )

    def _spread_for(min_interval_s: float) -> float:
        if args.poll_spread_s is not None:
            return max(0.0, args.poll_spread_s)
        return max(0.0, min_interval_s / max(1, len(accounts)))

    gql_spread_s = _spread_for(args.min_interval_s)
    home_spread_s = _spread_for(args.home_min_interval_s)
    notifications_spread_s = _spread_for(args.notifications_min_interval_s)

    rotator = AccountRotator(
        accounts,
        min_interval_s=args.min_interval_s,
        jitter_s=args.jitter_s,
        initial_stagger_s=gql_spread_s,
        on_account_dead=on_account_dead,
    )
    home_rotator = AccountRotator(
        accounts,
        min_interval_s=args.home_min_interval_s,
        jitter_s=args.jitter_s,
        initial_stagger_s=home_spread_s,
        on_account_dead=on_account_dead,
    )
    notifications_rotator = AccountRotator(
        accounts,
        min_interval_s=args.notifications_min_interval_s,
        jitter_s=args.jitter_s,
        initial_stagger_s=notifications_spread_s,
        on_account_dead=on_account_dead,
    )
    rest_rotator = AccountRotator(
        accounts,
        min_interval_s=args.min_interval_s,
        jitter_s=args.jitter_s,
        initial_stagger_s=gql_spread_s,
        on_account_dead=on_account_dead,
    )

    # ── hash registry (auto-refresh) ──────────────────────────────────────
    hash_registry = None
    if not args.no_hash_refresh:
        hash_registry = HashRegistry(
            initial={
                "UserTweets": _GQL_HASH,
                "HomeLatestTimeline": _HOME_LATEST_HASH,
            },
            on_change=lambda op, old, new: health.alert(
                level="warn",
                code=f"hash_changed_{op}",
                message=f"GQL hash rotated for {op}: {old or '(none)'} -> {new}",
            ),
        )
        hash_registry.start()

    # ── checkpoint (restore + periodic save) ──────────────────────────────
    checkpoint = None
    if not args.no_checkpoint:
        def _build_state():
            return {
                "user_id": user_id,
                "username": args.username,
                "seen_tweet_ids": dedup.export_seen(),
                "lifetime_count": health.lifetime_count()[0],
                "rotator_status": {
                    label: {
                        "dead": b.get("dead", False),
                        "dead_reason": b.get("dead_reason", ""),
                        "remaining": b.get("remaining"),
                    }
                    for label, b in rotator.status().get("budget", {}).items()
                },
                "hashes": hash_registry.all() if hash_registry else {
                    "UserTweets": _GQL_HASH,
                    "HomeLatestTimeline": _HOME_LATEST_HASH,
                },
            }

        checkpoint = Checkpoint(get_state_fn=_build_state, path=args.checkpoint_file)
        prev = checkpoint.load()
        if prev:
            dedup.restore_seen(prev.get("seen_tweet_ids", {}))
            print(f"[checkpoint] restored {len(dedup._seen)} seen tweet ids from {args.checkpoint_file}")
        checkpoint.start()

    # ── alerts + health.json writer ───────────────────────────────────────
    alert_sink = None
    health_monitor = None
    if not args.no_alerts:
        alert_sink = AlertSink()
        alert_sink.start()
        health_monitor = HealthMonitor(
            health=health,
            sink=alert_sink,
            health_file=args.health_file,
            alert_no_signal_s=args.alert_no_signal_s,
        )
        health_monitor.start()
        if alert_sink.enabled:
            print(f"[alerts] webhook enabled, health -> {args.health_file}")
        else:
            print(f"[alerts] webhook NOT configured (set POLYBOT_DISCORD_WEBHOOK or "
                  f"POLYBOT_TELEGRAM_BOT_TOKEN), health -> {args.health_file}")

    gql_count: list[Optional[int]] = [None]
    gql_count_ts: list[Optional[float]] = [None]
    home_count: list[Optional[int]] = [None]
    home_count_ts: list[Optional[float]] = [None]
    notifications_count: list[Optional[int]] = [None]
    notifications_count_ts: list[Optional[float]] = [None]

    def on_gql_count(n: int) -> None:
        gql_count[0] = n
        gql_count_ts[0] = time.time()

    def on_home_count(n: int) -> None:
        home_count[0] = n
        home_count_ts[0] = time.time()

    def on_notifications_count(n: int) -> None:
        notifications_count[0] = n
        notifications_count_ts[0] = time.time()

    # SSE is OPT-IN now — Twitter's live_pipeline doesn't push new tweets.
    # Only spin it up if --sse was explicitly requested.
    pool: Optional[MultiSSEPool] = None
    if args.sse and not args.no_sse:
        pool = MultiSSEPool(
            accounts,
            watch_user_id=user_id,
            on_tweet=dedup.add,
        )
        pool.start()

    pw_client = None
    if args.playwright:
        try:
            from signals.sse_playwright import PlaywrightSSEClient
            acc0 = accounts[0]
            pw_client = PlaywrightSSEClient(
                bearer=acc0.bearer,
                cookie=acc0.cookie,
                csrf=acc0.csrf,
                watch_user_id=user_id,
                on_tweet=dedup.add,
                method_label=f"pw/{acc0.label}",
                log=__import__("logging").getLogger("sse_playwright"),
            )
            pw_client.start()
            print("[playwright] SSE interception starting (headless Chromium)...")
        except Exception as e:
            print(f"[playwright] failed to start: {e}")

    # Thread factories — wrap so watchdog can re-spawn
    def _spawn_gql() -> threading.Thread:
        t = threading.Thread(
            target=_gql_poll_loop,
            kwargs=dict(
                user_id=user_id,
                accounts=accounts,
                rotator=rotator,
                on_count=on_gql_count,
                stop_evt=stop_evt,
                dedup=dedup,
                health=health,
                hash_registry=hash_registry,
            ),
            daemon=True,
            name="gql_poll",
        )
        t.start()
        return t

    def _spawn_home() -> threading.Thread:
        t = threading.Thread(
            target=_home_timeline_poll_loop,
            kwargs=dict(
                user_id=user_id,
                accounts=accounts,
                rotator=home_rotator,
                on_count=on_home_count,
                stop_evt=stop_evt,
                dedup=dedup,
                request_context=args.home_request_context,
                health=health,
                hash_registry=hash_registry,
            ),
            daemon=True,
            name="home_poll",
        )
        t.start()
        return t

    def _spawn_notifications() -> threading.Thread:
        t = threading.Thread(
            target=_notifications_poll_loop,
            kwargs=dict(
                username=args.username,
                user_id=user_id,
                accounts=accounts,
                rotator=notifications_rotator,
                on_count=on_notifications_count,
                stop_evt=stop_evt,
                dedup=dedup,
                health=health,
            ),
            daemon=True,
            name="notifications_poll",
        )
        t.start()
        return t

    def _spawn_rest() -> threading.Thread:
        t = threading.Thread(
            target=_rest_poll_loop,
            kwargs=dict(
                user_id=user_id,
                accounts=accounts,
                rotator=rest_rotator,
                stop_evt=stop_evt,
                dedup=dedup,
                rest_status=rest_status,
            ),
            daemon=True,
            name="rest_poll",
        )
        t.start()
        return t

    # ── Revive watcher: live-reload refreshed cookies ─────────────────────
    from signals.revive_watcher import ReviveWatcher
    revive = ReviveWatcher(
        revive_file="data/revive_accounts.txt",
        accounts_file=str(Path(__file__).resolve().parent.parent / "accounts.json"),
        accounts_list=accounts,
        rotators=[rotator, home_rotator, notifications_rotator, rest_rotator],
        on_revive=lambda label: health.alert(
            level="info",
            code=f"account_revived_{label}",
            message=f"Account {label} revived via cookie refresh",
        ),
    )
    revive.start()

    # ── XTracker backup poller (independent of Twitter accounts) ──────────
    xtracker = None
    if not args.no_xtracker:
        if args.username == args.xtracker_username:
            xtracker = XTrackerPoller(
                health=health,
                username=args.xtracker_username,
                interval_s=args.xtracker_interval_s,
            )
            xtracker.start()
        else:
            print(f"[xtracker] skipped: only tracks @{args.xtracker_username}, "
                  f"but watching @{args.username}")

    # ── Watchdog: auto-restart dead/zombie threads ────────────────────────
    watchdog = None
    if not args.no_watchdog:
        watchdog = ThreadWatchdog(
            health=health,
            check_interval_s=5.0,
        )
        if not args.no_gql:
            watchdog.register("gql", factory=_spawn_gql, max_heartbeat_age_s=90.0)
        if args.home_timeline:
            watchdog.register("home", factory=_spawn_home, max_heartbeat_age_s=90.0)
        if args.notifications:
            watchdog.register("notifications", factory=_spawn_notifications, max_heartbeat_age_s=120.0)
        if args.rest:
            watchdog.register("rest", factory=_spawn_rest, max_heartbeat_age_s=90.0)
        watchdog.start()
    else:
        # No watchdog — start threads directly
        if not args.no_gql:
            _spawn_gql()
        if args.home_timeline:
            _spawn_home()
        if args.notifications:
            _spawn_notifications()
        if args.rest:
            _spawn_rest()

    if not args.rest:
        rest_status["disabled"] = True
    home_status: dict = {"disabled": not args.home_timeline}
    notifications_status: dict = {"disabled": not args.notifications}

    started_at = time.time()
    # Effective interval = per-account interval / accounts.
    eff_interval = args.min_interval_s / max(1, len(accounts))
    home_eff_interval = args.home_min_interval_s / max(1, len(accounts))
    labels = ", ".join(a.label for a in accounts)
    print(f"\nWatching @{args.username} (uid={user_id})  |  {len(accounts)} accounts: [{labels}]")
    avg_interval = args.min_interval_s + args.jitter_s / 2.0
    home_avg_interval = args.home_min_interval_s + args.jitter_s / 2.0
    print(f"  Per-account interval: {args.min_interval_s:.1f}-{args.min_interval_s+args.jitter_s:.1f}s "
          f"(avg {avg_interval:.1f}s, jitter ±{args.jitter_s/2:.1f}s)  →  "
          f"~{60.0/avg_interval:.1f} req/min/account")
    print(f"  GQL spread: {gql_spread_s:.2f}s  |  Effective check interval: ~{eff_interval:.2f}s  |  Target latency: ~{eff_interval/2:.2f}s avg")
    if args.home_timeline:
        print(
            f"  HomeLatestTimeline lane: ENABLED (accounts should follow @{args.username})  |  "
            f"context={args.home_request_context}  |  "
            f"per-account {args.home_min_interval_s:.1f}-{args.home_min_interval_s+args.jitter_s:.1f}s "
            f"(avg {home_avg_interval:.1f}s)  |  spread {home_spread_s:.2f}s  |  "
            f"effective ~{home_eff_interval:.2f}s"
        )
    if args.notifications:
        notifications_eff_interval = args.notifications_min_interval_s / max(1, len(accounts))
        print(
            f"  Notifications lane: ENABLED via Playwright /notifications  |  "
            f"per-account {args.notifications_min_interval_s:.1f}-{args.notifications_min_interval_s+args.jitter_s:.1f}s  |  "
            f"spread {notifications_spread_s:.2f}s  |  effective ~{notifications_eff_interval:.2f}s"
        )
    if args.rest:
        print(f"  REST lane: ENABLED (separate rate-limit bucket)")
    print("(first tweet detection fires after next poll cycle)\n")

    # ── signal handlers (Ctrl+C and SIGTERM for systemd) ──────────────────
    import signal as _signal
    def _sig_handler(signum, frame):
        health.request_stop()
        stop_evt.set()
    try:
        _signal.signal(_signal.SIGTERM, _sig_handler)
    except Exception:
        pass
    try:
        _signal.signal(_signal.SIGINT, _sig_handler)
    except Exception:
        pass

    try:
        while not stop_evt.is_set():
            time.sleep(1.0)
            pool_state = pool.state() if pool else {"alive": 0, "total": 0, "seen_tweets": 0}
            detections, total = recorder.snapshot()
            observations = recorder.observations()
            rot_status = rotator.status()

            gql_age = None
            if gql_count_ts[0] is not None:
                gql_age = round(time.time() - gql_count_ts[0], 1)

            home_age = None
            if home_count_ts[0] is not None:
                home_age = round(time.time() - home_count_ts[0], 1)

            notifications_age = None
            if notifications_count_ts[0] is not None:
                notifications_age = round(time.time() - notifications_count_ts[0], 1)

            home_rot_status = home_rotator.status()
            home_rot_status["disabled"] = home_status.get("disabled", False)
            notifications_rot_status = notifications_rotator.status()
            notifications_rot_status["disabled"] = notifications_status.get("disabled", False)
            rest_rot_status = rest_rotator.status()
            rest_rot_status["disabled"] = rest_status.get("disabled", False)
            _clear()
            print(_render(
                username=args.username,
                user_id=user_id,
                pool_state=pool_state,
                rotator_status=rot_status,
                home_rotator_status=home_rot_status,
                notifications_rotator_status=notifications_rot_status,
                rest_rotator_status=rest_rot_status,
                detections=detections,
                observations=observations,
                total=total,
                gql_count=gql_count[0],
                gql_age_s=gql_age,
                home_count=home_count[0],
                home_age_s=home_age,
                notifications_count=notifications_count[0],
                notifications_age_s=notifications_age,
                started_at=started_at,
                pw_state=pw_client.state() if pw_client else None,
                health_snapshot=health.snapshot(),
                xtracker_state=xtracker.state() if xtracker else None,
                hash_status=hash_registry.status() if hash_registry else None,
            ))
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        health.request_stop()
        if pool:
            pool.stop()
        if pw_client:
            pw_client.stop()
        if xtracker is not None:
            xtracker.stop()
        try:
            revive.stop()
        except Exception:
            pass
        if hash_registry is not None:
            hash_registry.stop()
        if watchdog is not None:
            watchdog.stop()
        if checkpoint is not None:
            checkpoint.stop()
        if health_monitor is not None:
            health_monitor.stop()
        if alert_sink is not None:
            alert_sink.stop()
        # remove lockfile
        if lockfile and os.path.exists(lockfile):
            try:
                os.remove(lockfile)
            except Exception:
                pass

    print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
