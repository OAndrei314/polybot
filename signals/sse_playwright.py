"""
Playwright-based SSE client for Twitter live_pipeline.

Runs a headless Chromium browser with the account's cookies,
intercepts Twitter's own SSE connection (which succeeds where
pure-Python SSE fails because x-client-transaction-id is handled
natively by the browser), and forwards tweet events to Python.

Twitter's live_pipeline SSE:
  - Topic /live_content/{auth_user_id}: home-feed live events
  - Confirmed to push /tweet_engagement/* frames for visible tweets
  - Hypothesis: also pushes new tweet frames when followed users tweet
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from typing import Callable, Optional

ELON_USER_ID = "44196397"
TWITTER_EPOCH_MS = 1288834974657


def _snowflake_unix(tweet_id) -> Optional[float]:
    try:
        tid = int(str(tweet_id))
    except Exception:
        return None
    if tid <= 0:
        return None
    return ((tid >> 22) + TWITTER_EPOCH_MS) / 1000.0


def _user_id_from_cookie(cookie: str) -> str:
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("twid="):
            val = urllib.parse.unquote(part.split("=", 1)[1])
            if val.startswith("u="):
                return val[2:]
    return ""


def _parse_cookies_for_playwright(cookie_str: str) -> list[dict]:
    """Convert cookie string to Playwright cookie format."""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".x.com",
            "path": "/",
            # ct0 must NOT be httpOnly — JS needs to read it for x-csrf-token
            "httpOnly": name in ("auth_token", "kdt", "twid", "att"),
            "secure": True,
            "sameSite": "None",
        })
    return cookies


# Injected before page JS runs — patches window.fetch to intercept SSE stream
_INTERCEPT_SCRIPT = """
window.__pw_sse_frames = [];
window.__pw_sse_connected = false;
const _origFetch = window.fetch;
window.fetch = async function(...args) {
    const url = (typeof args[0] === 'string') ? args[0] : (args[0]?.url || '');
    const resp = await _origFetch.apply(this, args);
    if (url.includes('live_pipeline/events')) {
        window.__pw_sse_connected = true;
        const clone = resp.clone();
        (async () => {
            try {
                const reader = clone.body.getReader();
                const dec = new TextDecoder();
                let buf = '';
                while (true) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    buf += dec.decode(value, { stream: true });
                    const lines = buf.split('\\n');
                    buf = lines.pop();
                    for (const line of lines) {
                        if (line.startsWith('data:')) {
                            const data = line.slice(5).trim();
                            if (data && data !== 'ping')
                                window.__pw_sse_frames.push(data);
                        }
                    }
                }
            } catch(e) {}
        })();
    }
    return resp;
};
"""


class PlaywrightSSEClient:
    """
    Intercepts Twitter's native SSE stream via a headless Chromium browser.

    This approach bypasses x-client-transaction-id and Cloudflare checks
    because the real browser JS handles all auth automatically.

    Detects new tweets in /live_content/{user_id} SSE frames.
    Also serves as a fallback even if SSE doesn't push new tweets,
    since we can trigger periodic GQL calls from the browser context.
    """

    def __init__(
        self,
        bearer: str,
        cookie: str,
        csrf: str = "",
        watch_user_id: str = ELON_USER_ID,
        on_tweet: Optional[Callable[[dict], None]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        method_label: str = "playwright_sse",
        log: Optional[logging.Logger] = None,
    ):
        self.bearer = bearer
        self.cookie = cookie
        self.watch_user_id = str(watch_user_id)
        self._auth_user_id = _user_id_from_cookie(cookie)
        self.on_tweet = on_tweet or (lambda d: None)
        self.on_event = on_event or (lambda d: None)
        self.method_label = method_label
        self.log = log or logging.getLogger("sse_playwright")
        self._stop = threading.Event()
        self._seen: set[str] = set()
        self._thread: Optional[threading.Thread] = None
        self._connected_at: Optional[float] = None
        self._last_frame_at: Optional[float] = None
        self._frame_count = 0

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def state(self) -> dict:
        return {
            "method": self.method_label,
            "alive": self.is_alive(),
            "connected_at": self._connected_at,
            "last_frame_at": self._last_frame_at,
            "seen": len(self._seen),
            "frames": self._frame_count,
        }

    def _handle_frame(self, raw: str):
        now = time.time()
        self._last_frame_at = now
        self._frame_count += 1
        try:
            obj = json.loads(raw)
        except Exception:
            return
        topic = obj.get("topic", "")
        self.log.debug("SSE frame topic=%s len=%d", topic, len(raw))
        self.on_event({
            "kind": "frame",
            "topic": topic,
            "method": self.method_label,
            "ts": now,
        })
        # Check for tweet content in the frame (any topic)
        self._scan_for_tweets(obj, now)

    def _scan_for_tweets(self, obj, now: float):
        """Recursively scan a JSON frame for tweet objects authored by watch_user_id."""
        if isinstance(obj, dict):
            legacy = obj.get("legacy")
            if isinstance(legacy, dict):
                tid = legacy.get("id_str")
                if tid and tid.isdigit() and tid not in self._seen:
                    # Check authorship
                    author_id = None
                    core = obj.get("core")
                    if isinstance(core, dict):
                        ur = core.get("user_results")
                        if isinstance(ur, dict):
                            r = ur.get("result")
                            if isinstance(r, dict):
                                author_id = r.get("rest_id")
                    if not author_id:
                        author_id = legacy.get("user_id_str") or obj.get("user_id_str")
                    if str(author_id) == self.watch_user_id:
                        # Reject retweets and replies
                        if not legacy.get("in_reply_to_status_id_str") and \
                           not legacy.get("retweeted_status_id_str") and \
                           "retweeted_status_result" not in obj:
                            self._seen.add(tid)
                            snow = _snowflake_unix(tid)
                            lat_ms = round((now - snow) * 1000.0, 1) if snow else None
                            self.on_tweet({
                                "tweet_id": tid,
                                "method": self.method_label,
                                "detected_at": now,
                                "snowflake_unix": snow,
                                "latency_ms": lat_ms,
                                "text": (legacy.get("full_text") or "")[:280],
                            })
            for v in obj.values():
                self._scan_for_tweets(v, now)
        elif isinstance(obj, list):
            for item in obj:
                self._scan_for_tweets(item, now)

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log.error("playwright not installed — pip install playwright")
            return

        pw_cookies = _parse_cookies_for_playwright(self.cookie)
        backoff = 5.0

        while not self._stop.is_set():
            try:
                self.on_event({
                    "kind": "connecting",
                    "method": self.method_label,
                    "ts": time.time(),
                })
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    ctx = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        )
                    )
                    ctx.add_cookies(pw_cookies)
                    ctx.add_init_script(_INTERCEPT_SCRIPT)
                    page = ctx.new_page()

                    try:
                        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        self.log.warning("page.goto error: %s", e)

                    # Wait for SSE to connect (up to 15s)
                    connected = False
                    deadline = time.time() + 15
                    while time.time() < deadline and not self._stop.is_set():
                        if page.evaluate("window.__pw_sse_connected"):
                            connected = True
                            break
                        time.sleep(0.3)

                    if not connected:
                        self.log.warning("SSE did not connect within 15s")
                        browser.close()
                        time.sleep(backoff)
                        backoff = min(60.0, backoff * 2)
                        continue

                    self._connected_at = time.time()
                    backoff = 5.0
                    self.log.info("SSE connected via Playwright, auth_user_id=%s", self._auth_user_id)
                    self.on_event({
                        "kind": "connected",
                        "method": self.method_label,
                        "ts": self._connected_at,
                    })

                    # Poll SSE frame buffer while browser runs
                    while not self._stop.is_set():
                        time.sleep(0.05)  # 50ms poll of JS buffer
                        try:
                            frames = page.evaluate("window.__pw_sse_frames.splice(0)")
                        except Exception:
                            break
                        for raw in frames:
                            self._handle_frame(raw)

                    browser.close()

            except Exception as e:
                self.log.exception("Playwright SSE loop crash: %s", e)
                self.on_event({
                    "kind": "error",
                    "error": str(e)[:200],
                    "method": self.method_label,
                    "ts": time.time(),
                })

            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    def start(self):
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pw_sse", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
