from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import requests

ELON_USER_ID = "44196397"
ELON_USERNAME = "elonmusk"
QUERY_HASH = "Y59DTUMfcKmUAATiT2SlTw"
XTRACKER_BASE = "https://xtracker.polymarket.com/api"


def _load_dotenv_fallback(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _find_statuses_count(obj: Any) -> int | None:
    if isinstance(obj, dict):
        if "statuses_count" in obj:
            try:
                return int(obj["statuses_count"])
            except Exception:
                return None
        for value in obj.values():
            found = _find_statuses_count(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_statuses_count(value)
            if found is not None:
                return found
    return None


class TweetStream:
    """Primary: browser SSE. Fallbacks preserved: GraphQL + XTracker + RSS log."""

    def __init__(self, rss_log_path: str = "rss_signal_events.jsonl") -> None:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except Exception:
            _load_dotenv_fallback()
        self.rss_log_path = rss_log_path
        self.session = requests.Session()
        self.headers = {
            "authorization": os.getenv("TWITTER_BEARER", ""),
            "x-csrf-token": os.getenv("TWITTER_CSRF", ""),
            "cookie": os.getenv("TWITTER_COOKIE", ""),
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "x-twitter-auth-type": "OAuth2Session",
            "user-agent": "Mozilla/5.0",
        }
        self._last_count: int | None = None
        self._last_source = "none"
        self._last_count_unix: float | None = None
        self._latest_tweet: dict[str, Any] = {}
        # Provisional period-count tracking: when xtracker lags, advance the
        # last real count by the lifetime-count delta seen since that fetch.
        # This cuts the ~3-minute blind window we observed in the May-02 run.
        self._xtracker_anchor: dict[str, tuple[int, int | None]] = {}  # slug→(count, lifetime)

    def start(self) -> None:
        try:
            # Must be set before importing sse_playright so module-level logger is quiet.
            os.environ["POLYBOT_QUIET"] = "1"
            from signals.sse_browser import configure_watch, start_sse_browser
            configure_watch(username=ELON_USERNAME, user_id=ELON_USER_ID)
            start_sse_browser()
        except Exception:
            # SSE stays optional; graph/xtracker/rss still work.
            pass

    def _graphql_count(self) -> int | None:
        variables = {
            "userId": ELON_USER_ID,
            "count": 1,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": False,
            "withVoice": False,
            "withV2Timeline": True,
        }
        features = {
            "rweb_tipjar_consumption_enabled": False,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "articles_preview_enabled": True,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": False,
            "responsive_web_enhance_cards_enabled": False,
        }

        url = (
            f"https://x.com/i/api/graphql/{QUERY_HASH}/UserTweets"
            f"?variables={urllib.parse.quote(json.dumps(variables))}"
            f"&features={urllib.parse.quote(json.dumps(features))}"
        )

        try:
            resp = self.session.get(url, headers=self.headers, timeout=6)
            if not resp.ok:
                return None
            return _find_statuses_count(resp.json())
        except Exception:
            return None

    def _sse_state(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ready": False,
            "count": None,
            "count_source": None,
            "heartbeat_age_s": None,
            "latest_signal": {},
            "watch_info": {},
        }
        try:
            from signals.sse_browser import (
                get_watch_info,
                get_count_source,
                get_last_event_update,
                get_last_update,
                get_latest_signal,
                get_tweet_count_sse,
                is_ready,
            )

            out["ready"] = bool(is_ready())
            out["count"] = get_tweet_count_sse()
            out["count_source"] = get_count_source()
            hb = get_last_event_update()
            if hb:
                out["heartbeat_age_s"] = round(time.time() - float(hb), 3)
            lu = get_last_update()
            if lu:
                out["last_update_age_s"] = round(time.time() - float(lu), 3)
            out["latest_signal"] = get_latest_signal() or {}
            out["watch_info"] = get_watch_info() or {}
        except Exception:
            pass
        return out

    def _xtracker_trackings(self) -> dict[str, dict[str, Any]]:
        by_slug: dict[str, dict[str, Any]] = {}
        try:
            resp = self.session.get(
                f"{XTRACKER_BASE}/users/{ELON_USERNAME}/trackings?activeOnly=true",
                timeout=5,
            )
            if not resp.ok:
                return by_slug
            payload = resp.json()
            for tr in payload.get("data") or []:
                link = str(tr.get("marketLink") or "")
                slug = link.rsplit("/", 1)[-1] if link else ""
                if slug:
                    by_slug[slug] = tr
        except Exception:
            return by_slug
        return by_slug

    def _xtracker_period_count(self, start: datetime, end: datetime) -> int | None:
        url = (
            f"{XTRACKER_BASE}/users/{ELON_USERNAME}/posts"
            f"?startDate={urllib.parse.quote(_iso_z(start))}"
            f"&endDate={urllib.parse.quote(_iso_z(end))}"
        )
        try:
            resp = self.session.get(url, timeout=5)
            if not resp.ok:
                return None
            payload = resp.json()
            posts = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(posts, list):
                return len(posts)
        except Exception:
            return None
        return None

    def _read_rss_latest(self) -> dict[str, Any]:
        if not os.path.exists(self.rss_log_path):
            return {}
        try:
            with open(self.rss_log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 32_000), os.SEEK_SET)
                chunk = f.read().decode("utf-8", errors="ignore")
            lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
            if not lines:
                return {}
            row = json.loads(lines[-1])
            ts = row.get("detected_at")
            dt = _parse_iso(ts)
            age = None
            if dt is not None:
                age = (datetime.now(timezone.utc) - dt).total_seconds()
            return {
                "tweet_id": row.get("tweet_id"),
                "tweet_text": row.get("title"),
                "detected_at": ts,
                "age_s": round(age, 3) if age is not None else None,
            }
        except Exception:
            return {}

    def poll(self) -> dict[str, Any]:
        sse = self._sse_state()
        lifetime_count = None
        source = "none"
        fresh = False

        if sse.get("ready") and sse.get("count") is not None:
            lifetime_count = int(sse["count"])
            source = str(sse.get("count_source") or "sse")
            self._latest_tweet = dict(sse.get("latest_signal") or {})
            hb = sse.get("heartbeat_age_s")
            lu = sse.get("last_update_age_s")
            fresh = (
                (isinstance(hb, (int, float)) and float(hb) < 60.0)
                or (isinstance(lu, (int, float)) and float(lu) < 60.0)
            )

        if lifetime_count is None or not fresh:
            gql = self._graphql_count()
            if gql is not None:
                if lifetime_count is None or int(gql) >= lifetime_count:
                    lifetime_count = int(gql)
                    source = "graphql"
                fresh = True

        if lifetime_count is not None:
            moved = self._last_count != lifetime_count
            self._last_count = lifetime_count
            self._last_source = source
            if moved or fresh:
                self._last_count_unix = time.time()

        if self._latest_tweet.get("tweet_text") is None:
            rss_latest = self._read_rss_latest()
            if rss_latest:
                self._latest_tweet = rss_latest

        trackings = self._xtracker_trackings()
        raw_xtracker: dict[str, int | None] = {}
        for slug, tr in trackings.items():
            start = _parse_iso(tr.get("startDate"))
            end = _parse_iso(tr.get("endDate"))
            if start is None or end is None:
                raw_xtracker[slug] = None
                continue
            raw_xtracker[slug] = self._xtracker_period_count(start, end)

        # Build provisional period counts: when xtracker hasn't updated yet,
        # advance the last real count by the lifetime-count delta since that
        # fetch. This narrows the ~3-minute lag seen in the 2026-05-02 live run
        # where the market repriced 3 minutes before our period counts caught up.
        provisional: dict[str, int | None] = {}
        for slug, real in raw_xtracker.items():
            if real is not None:
                self._xtracker_anchor[slug] = (real, lifetime_count)
                provisional[slug] = real
            elif slug in self._xtracker_anchor:
                anchor_count, anchor_lifetime = self._xtracker_anchor[slug]
                if anchor_lifetime is not None and lifetime_count is not None:
                    delta = max(0, lifetime_count - anchor_lifetime)
                    provisional[slug] = anchor_count + delta
                else:
                    provisional[slug] = anchor_count
            else:
                provisional[slug] = None

        count_age_s = None
        if self._last_count_unix is not None:
            count_age_s = round(time.time() - self._last_count_unix, 3)

        return {
            "lifetime_count": self._last_count,
            "count_source": self._last_source,
            "count_age_s": count_age_s,
            "latest_tweet": dict(self._latest_tweet),
            "sse": sse,
            "xtracker_period_counts": provisional,
            "rss_latest": self._read_rss_latest(),
        }


if __name__ == "__main__":
    stream = TweetStream()
    stream.start()
    while True:
        print(json.dumps(stream.poll(), indent=2))
        time.sleep(5)
