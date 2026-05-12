"""
Pure-Python direct SSE client for the X (Twitter) live_pipeline.

No Playwright, no Chromium. Just httpx streaming with a logged-in session
(TWITTER_BEARER + TWITTER_COOKIE + TWITTER_CSRF). Subscribes to
user_timeline:<user_id> and emits tweet detections with high-resolution
local timestamps for latency benchmarking.

Subscription flow (mirrors what x.com does in-browser):
  1. GET  https://api.x.com/live_pipeline/events?topic=/system/config
  2. Parse first SSE frame for payload.config.session_id
  3. POST https://api.x.com/1.1/live_pipeline/update_subscriptions
         with header  livepipeline-session: <session_id>
         and   body   sub_topics=user_timeline:<user_id>
  4. Continue reading SSE for user_timeline:* frames.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import httpx

TWITTER_EPOCH_MS = 1288834974657
ELON_USER_ID = "44196397"

SSE_URL = "https://api.x.com/live_pipeline/events?topic=%2Fsystem%2Fconfig"
SUBSCRIBE_URL = "https://api.x.com/1.1/live_pipeline/update_subscriptions"


def snowflake_unix(tweet_id) -> Optional[float]:
    try:
        tid = int(str(tweet_id))
    except Exception:
        return None
    if tid <= 0:
        return None
    return ((tid >> 22) + TWITTER_EPOCH_MS) / 1000.0


def _ct0_from_cookie(cookie: str) -> str:
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("ct0="):
            return part.split("=", 1)[1]
    return ""


def _is_authored(tweet_obj, watch_user_id: str) -> bool:
    if not isinstance(tweet_obj, dict):
        return False
    legacy = tweet_obj.get("legacy")
    if not isinstance(legacy, dict):
        return False
    tid = legacy.get("id_str")
    if not (isinstance(tid, str) and tid.isdigit()):
        return False
    if legacy.get("in_reply_to_status_id_str"):
        return False
    if legacy.get("retweeted_status_id_str") or "retweeted_status_result" in tweet_obj:
        return False
    author_id = None
    core = tweet_obj.get("core")
    if isinstance(core, dict):
        ur = core.get("user_results")
        if isinstance(ur, dict):
            r = ur.get("result")
            if isinstance(r, dict):
                author_id = r.get("rest_id")
    if not author_id:
        author_id = legacy.get("user_id_str") or tweet_obj.get("user_id_str")
    return str(author_id) == str(watch_user_id)


def _extract_tweets(obj, watch_user_id: str, out: dict):
    if isinstance(obj, dict):
        if _is_authored(obj, watch_user_id):
            legacy = obj["legacy"]
            out[legacy["id_str"]] = legacy.get("full_text", "")
        for v in obj.values():
            _extract_tweets(v, watch_user_id, out)
    elif isinstance(obj, list):
        for item in obj:
            _extract_tweets(item, watch_user_id, out)


class DirectSSEClient:
    """Run SSE in a daemon thread. on_tweet/on_event are best-effort callbacks."""

    def __init__(
        self,
        bearer: str,
        cookie: str,
        csrf: str = "",
        watch_user_id: str = ELON_USER_ID,
        on_tweet: Optional[Callable[[dict], None]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        method_label: str = "direct_sse",
        log: Optional[logging.Logger] = None,
    ):
        bearer = (bearer or "").strip()
        if bearer and not bearer.lower().startswith("bearer "):
            bearer = f"Bearer {bearer}"
        self.bearer = bearer
        self.cookie = cookie
        self.csrf = csrf or _ct0_from_cookie(cookie)
        self.watch_user_id = str(watch_user_id)
        self.on_tweet = on_tweet or (lambda d: None)
        self.on_event = on_event or (lambda d: None)
        self.method_label = method_label
        self.log = log or logging.getLogger("sse_direct")
        self._stop = threading.Event()
        self._seen: set[str] = set()
        self._session_id: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._connected_at: Optional[float] = None
        self._last_frame_at: Optional[float] = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def state(self) -> dict:
        return {
            "method": self.method_label,
            "alive": self.is_alive(),
            "connected_at": self._connected_at,
            "last_frame_at": self._last_frame_at,
            "session_id": self._session_id,
            "seen": len(self._seen),
        }

    def _sse_headers(self) -> dict:
        return {
            "authorization": self.bearer,
            "x-csrf-token": self.csrf,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "accept": "text/event-stream",
            "cache-control": "no-cache",
            "referer": "https://x.com/",
            "cookie": self.cookie,
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        }

    def _subscribe(self):
        sid = self._session_id
        if not sid:
            return
        ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )
        headers = {
            "authorization": self.bearer,
            "x-csrf-token": self.csrf,
            "content-type": "application/x-www-form-urlencoded",
            "livepipeline-session": sid,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "origin": "https://x.com",
            "referer": "https://x.com/",
            "cookie": self.cookie,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": ua,
        }
        bodies = [
            f"sub_topics=user_timeline%3A{self.watch_user_id}&unsub_topics=",
            f"subprotocol=updater-v2&sub=user_timeline%3A{self.watch_user_id}&unsub=",
        ]
        urls = [
            SUBSCRIBE_URL,
            "https://api.twitter.com/1.1/live_pipeline/update_subscriptions",
        ]
        ok_any = False
        last_status = None
        for url in urls:
            for body in bodies:
                try:
                    with httpx.Client(timeout=15.0) as c:
                        r = c.post(url, headers=headers, content=body)
                    last_status = r.status_code
                    self.log.info(
                        "subscribe url=%s status=%s body=%s",
                        url.rsplit("/", 2)[-2:],
                        r.status_code,
                        (r.text or "")[:160],
                    )
                    if r.status_code == 200:
                        ok_any = True
                        break
                except Exception as e:
                    self.log.warning("subscribe error: %s", e)
            if ok_any:
                break
        self.on_event({
            "kind": "subscribed",
            "status": last_status,
            "ok": ok_any,
            "method": self.method_label,
            "ts": time.time(),
        })

    def _handle_data(self, raw: str):
        try:
            obj = json.loads(raw)
        except Exception:
            return
        topic = obj.get("topic", "")
        now = time.time()
        self._last_frame_at = now
        evt = {
            "kind": "frame",
            "topic": topic,
            "method": self.method_label,
            "ts": now,
        }
        if topic == "/system/subscriptions":
            payload = obj.get("payload") or {}
            try:
                evt["subscriptions"] = json.dumps(payload, separators=(",", ":"))[:500]
            except Exception:
                pass
        self.on_event(evt)
        if topic == "/system/config" and not self._session_id:
            sid = (
                obj.get("payload", {})
                .get("config", {})
                .get("session_id")
            )
            if sid:
                self._session_id = sid
                self.log.info("session_id=%s", sid)
                threading.Thread(
                    target=self._subscribe, name="sse_direct_sub", daemon=True
                ).start()
            return
        if "user_timeline" in topic:
            tweets: dict = {}
            _extract_tweets(obj, self.watch_user_id, tweets)
            for tid, text in tweets.items():
                if tid in self._seen:
                    continue
                self._seen.add(tid)
                snow = snowflake_unix(tid)
                lat_ms = round((now - snow) * 1000.0, 1) if snow is not None else None
                self.on_tweet({
                    "tweet_id": tid,
                    "method": self.method_label,
                    "detected_at": now,
                    "snowflake_unix": snow,
                    "latency_ms": lat_ms,
                    "text": (text or "")[:280],
                })

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session_id = None
                self.on_event({
                    "kind": "connecting",
                    "method": self.method_label,
                    "ts": time.time(),
                })
                timeout = httpx.Timeout(connect=10.0, read=None, write=15.0, pool=10.0)
                with httpx.Client(timeout=timeout, http2=False) as client:
                    with client.stream("GET", SSE_URL, headers=self._sse_headers()) as r:
                        if r.status_code != 200:
                            body = ""
                            try:
                                body = r.read().decode("utf-8", "ignore")[:200]
                            except Exception:
                                pass
                            self.log.warning(
                                "SSE bad status=%s body=%s",
                                r.status_code,
                                body,
                            )
                            self.on_event({
                                "kind": "status_error",
                                "status": r.status_code,
                                "body": body,
                                "method": self.method_label,
                                "ts": time.time(),
                            })
                            time.sleep(min(60.0, backoff))
                            backoff = min(60.0, backoff * 2)
                            continue
                        self._connected_at = time.time()
                        self.on_event({
                            "kind": "connected",
                            "method": self.method_label,
                            "ts": self._connected_at,
                        })
                        backoff = 1.0
                        buf = ""
                        for chunk in r.iter_text():
                            if self._stop.is_set():
                                break
                            buf += chunk
                            while "\n\n" in buf:
                                block, buf = buf.split("\n\n", 1)
                                for line in block.split("\n"):
                                    if line.startswith("data:"):
                                        data = line[5:].strip()
                                        if data and data != "ping":
                                            self._handle_data(data)
            except httpx.HTTPError as e:
                self.log.warning("SSE http error: %s", e)
                self.on_event({
                    "kind": "http_error",
                    "error": str(e)[:200],
                    "method": self.method_label,
                    "ts": time.time(),
                })
            except Exception as e:
                self.log.exception("SSE loop crash: %s", e)
            if self._stop.is_set():
                break
            time.sleep(min(30.0, backoff))
            backoff = min(60.0, backoff * 2)

    def start(self):
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="sse_direct", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
