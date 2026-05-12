"""
sse_playwright.py
Headless Chromium intercepts Twitter's Live Pipeline SSE via fetch injection.
Uses page.expose_function to bypass console.log size limits.
"""

import asyncio
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote

QUIET_LOGS = os.getenv("POLYBOT_QUIET", "0") == "1"


def _p(*args, **kwargs):
    if not QUIET_LOGS:
        print(*args, **kwargs)


_live_count      = None
_live_count_lock = threading.Lock()
_last_update     = None
_last_count_source = None
_last_event_update = None
_last_signal_update = None
_last_signal_latency_ms = None
_last_signal_id = None
_last_signal_created_at = None
_last_signal_text = None
_event_times = deque(maxlen=50000)
_signal_times = deque(maxlen=10000)
_browser_ready   = False
_seen_tweet_ids = set()
_timeline_bootstrapped = False
_stream_started_at = None
_last_subscription_status = None
_last_subscription_at = None
_last_subscription_user_id = None
_last_subscription_body = None
_last_active_subscriptions = None
_last_search_poll_status = None
_last_search_poll_at = None
_last_search_poll_error = None
_last_search_poll_tweet_count = None
_last_notifications_poll_status = None
_last_notifications_poll_at = None
_last_notifications_poll_error = None
_last_notifications_poll_tweet_count = None
SIGNAL_LOG_FILE = os.getenv("POLYBOT_SIGNAL_LOG", "tweet_signal_events.jsonl")
REACTION_LOG_FILE = os.getenv("POLYBOT_REACTION_LOG", "bot_reaction_events.jsonl")
SSE_EVENT_LOG_FILE = os.getenv("POLYBOT_SSE_EVENT_LOG", "sse_event_frames.jsonl")
ROLE = os.getenv("POLYBOT_ROLE", "unknown")
JSONL_MAX_BYTES = int(float(os.getenv("POLYBOT_JSONL_MAX_BYTES", "800000")))
JSONL_KEEP_LINES = int(float(os.getenv("POLYBOT_JSONL_KEEP_LINES", "2000")))
JSONL_TRIM_INTERVAL_S = float(os.getenv("POLYBOT_JSONL_TRIM_INTERVAL_S", "15"))
_last_trim_unix_by_path = {}

ELON_USER_ID  = "44196397"
ELON_USERNAME = "elonmusk"
TWITTER_EPOCH_MS = 1288834974657
BOOTSTRAP_RECENCY_GRACE_SEC = float(os.getenv("POLYBOT_BOOTSTRAP_RECENCY_SEC", "10"))
ENABLE_SEARCH_POLL = os.getenv("POLYBOT_ENABLE_SEARCH_POLL", "0") == "1"
SEARCH_POLL_MS = int(float(os.getenv("POLYBOT_SEARCH_POLL_MS", "1000")))
ENABLE_NOTIFICATIONS_POLL = os.getenv("POLYBOT_ENABLE_NOTIFICATIONS_POLL", "1") == "1"
NOTIFICATIONS_POLL_MS = int(float(os.getenv("POLYBOT_NOTIFICATIONS_POLL_MS", "2500")))
_watch_user_id = ELON_USER_ID
_watch_username = ELON_USERNAME


def _tail_lines_bytes(path, keep_lines, chunk_size=65536):
    if keep_lines <= 0:
        return b""
    if not os.path.exists(path):
        return b""

    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        if pos <= 0:
            return b""

        buf = b""
        line_count = 0
        while pos > 0 and line_count <= keep_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos, os.SEEK_SET)
            chunk = f.read(read_size)
            buf = chunk + buf
            line_count = buf.count(b"\n")

    lines = buf.splitlines()[-keep_lines:]
    if not lines:
        return b""
    return b"\n".join(lines) + b"\n"


def _maybe_trim_jsonl(path):
    if JSONL_MAX_BYTES <= 0 or JSONL_KEEP_LINES <= 0:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size <= JSONL_MAX_BYTES:
        return

    now = time.time()
    last_trim = float(_last_trim_unix_by_path.get(path, 0.0))
    if (now - last_trim) < JSONL_TRIM_INTERVAL_S:
        return

    data = _tail_lines_bytes(path, JSONL_KEEP_LINES)
    with open(path, "wb") as f:
        f.write(data)
    _last_trim_unix_by_path[path] = now


def configure_watch(username=None, user_id=None):
    global _watch_user_id, _watch_username
    global _timeline_bootstrapped, _stream_started_at
    watch_username = username or ELON_USERNAME
    watch_user_id = str(user_id or ELON_USER_ID)
    with _live_count_lock:
        if str(_watch_user_id) != watch_user_id:
            _seen_tweet_ids.clear()
            _timeline_bootstrapped = False
        _watch_username = watch_username
        _watch_user_id = watch_user_id
        _stream_started_at = time.time()


def get_watch_info():
    with _live_count_lock:
        return {
            "username": _watch_username,
            "user_id": _watch_user_id,
            "stream_started_at": _stream_started_at,
            "subscription_status": _last_subscription_status,
            "subscription_at": _last_subscription_at,
            "subscription_user_id": _last_subscription_user_id,
            "subscription_body": _last_subscription_body,
            "active_subscriptions": _last_active_subscriptions,
            "search_poll_status": _last_search_poll_status,
            "search_poll_at": _last_search_poll_at,
            "search_poll_error": _last_search_poll_error,
            "search_poll_tweet_count": _last_search_poll_tweet_count,
            "notifications_poll_status": _last_notifications_poll_status,
            "notifications_poll_at": _last_notifications_poll_at,
            "notifications_poll_error": _last_notifications_poll_error,
            "notifications_poll_tweet_count": _last_notifications_poll_tweet_count,
            "seen_tweets": len(_seen_tweet_ids),
            "timeline_bootstrapped": _timeline_bootstrapped,
        }


def get_live_count():
    with _live_count_lock:
        return _live_count


def get_last_update():
    with _live_count_lock:
        return _last_update


def get_last_event_update():
    with _live_count_lock:
        return _last_event_update


def get_last_signal_update():
    with _live_count_lock:
        return _last_signal_update


def get_last_signal_latency_ms():
    with _live_count_lock:
        return _last_signal_latency_ms


def get_latest_signal():
    with _live_count_lock:
        return {
            "tweet_id": _last_signal_id,
            "tweet_created_at": _last_signal_created_at,
            "tweet_text": _last_signal_text,
            "detected_at_unix": _last_signal_update,
            "latency_ms": _last_signal_latency_ms,
        }


def mark_sse_event(topic=None):
    global _last_event_update
    now = time.time()
    with _live_count_lock:
        _last_event_update = now
        _event_times.append(now)
    _append_sse_event(event_type="frame", topic=topic)


def mark_signal_event(tweet_id=None, created_at=None, text=None):
    global _last_signal_update, _last_signal_latency_ms
    global _last_signal_id, _last_signal_created_at, _last_signal_text
    now = time.time()
    lat_ms = _compute_tweet_latency_ms(tweet_id, created_at, now)
    with _live_count_lock:
        _last_signal_update = now
        _last_signal_latency_ms = lat_ms
        _last_signal_id = str(tweet_id) if tweet_id is not None else None
        _last_signal_created_at = created_at
        _last_signal_text = text
        _signal_times.append(now)


def get_stream_stats(window_sec=4.0):
    now = time.time()
    cutoff = now - float(window_sec)
    with _live_count_lock:
        events = sum(1 for ts in _event_times if ts >= cutoff)
        signals = sum(1 for ts in _signal_times if ts >= cutoff)
        last_event_age = (now - _last_event_update) if _last_event_update else None
        last_signal_age = (now - _last_signal_update) if _last_signal_update else None
    return {
        "window_sec": float(window_sec),
        "events_in_window": events,
        "signals_in_window": signals,
        "events_per_sec": events / float(window_sec),
        "signals_per_sec": signals / float(window_sec),
        "last_event_age_sec": last_event_age,
        "last_signal_age_sec": last_signal_age,
    }


def _parse_twitter_created_at(raw):
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _tweet_id_created_unix(tweet_id):
    try:
        tid = int(str(tweet_id))
    except Exception:
        return None
    if tid <= 0:
        return None
    created_ms = (tid >> 22) + TWITTER_EPOCH_MS
    return created_ms / 1000.0


def _tweet_id_created_iso(tweet_id):
    ts = _tweet_id_created_unix(tweet_id)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _compute_tweet_latency_ms(tweet_id, created_at_raw, detect_unix_ts):
    tweet_id_ts = _tweet_id_created_unix(tweet_id)
    if tweet_id_ts is not None:
        ms = (detect_unix_ts - tweet_id_ts) * 1000.0
        return round(max(0.0, ms), 1)
    return _compute_created_at_latency_ms(created_at_raw, detect_unix_ts)


def _compute_created_at_latency_ms(created_at_raw, detect_unix_ts):
    dt = _parse_twitter_created_at(created_at_raw)
    if dt is None:
        return None
    ms = (detect_unix_ts - dt.timestamp()) * 1000.0
    if ms < 0:
        return 0.0
    return round(ms, 1)


def set_live_count(count, source="unknown"):
    global _live_count, _last_update, _last_count_source
    with _live_count_lock:
        old         = _live_count
        _live_count = count
        _last_update = time.time()
        _last_count_source = source
    return old


def get_count_source():
    with _live_count_lock:
        return _last_count_source


def _seed_count_from_baseline_file():
    try:
        with open(os.path.expanduser("~/polybot/baseline.json")) as f:
            data = json.load(f)
        life = data.get("lifetime_at_save")
        if isinstance(life, (int, float)):
            return int(life)
        base = data.get("baseline_lifetime")
        wk = data.get("weekly_at_save")
        if isinstance(base, (int, float)) and isinstance(wk, (int, float)):
            return int(round(base + wk))
    except Exception:
        return None
    return None


def _is_countable_tweet_obj(tweet_obj):
    """
    Count only original authored tweets:
    - author must be the watched account
    - ignore replies
    - ignore retweets
    """
    if not isinstance(tweet_obj, dict):
        return False

    legacy = tweet_obj.get("legacy")
    if not isinstance(legacy, dict):
        return False

    tweet_id = legacy.get("id_str")
    if not (isinstance(tweet_id, str) and tweet_id.isdigit()):
        return False

    # Exclude replies
    if legacy.get("in_reply_to_status_id_str"):
        return False

    # Exclude retweets
    if legacy.get("retweeted_status_id_str"):
        return False
    if "retweeted_status_result" in tweet_obj:
        return False

    # Author check
    author_id = None
    core = tweet_obj.get("core")
    if isinstance(core, dict):
        ur = core.get("user_results")
        if isinstance(ur, dict):
            result = ur.get("result")
            if isinstance(result, dict):
                author_id = result.get("rest_id")
    if not author_id:
        author_id = legacy.get("user_id_str") or tweet_obj.get("user_id_str")

    return str(author_id) == str(_watch_user_id)


def _extract_countable_tweet_ids(obj, out):
    if isinstance(obj, dict):
        if _is_countable_tweet_obj(obj):
            out.add(obj["legacy"]["id_str"])
        for v in obj.values():
            _extract_countable_tweet_ids(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _extract_countable_tweet_ids(item, out)


def _extract_countable_tweet_meta(obj, out):
    if isinstance(obj, dict):
        if _is_countable_tweet_obj(obj):
            legacy = obj.get("legacy", {})
            tid = legacy.get("id_str")
            if tid:
                out[tid] = {
                    "created_at": legacy.get("created_at"),
                    "full_text": legacy.get("full_text"),
                }
        for v in obj.values():
            _extract_countable_tweet_meta(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _extract_countable_tweet_meta(item, out)


def _process_countable_tweets_from_obj(obj, source, watch_username=None):
    global _timeline_bootstrapped

    ids = set()
    meta = {}
    _extract_countable_tweet_ids(obj, ids)
    _extract_countable_tweet_meta(obj, meta)
    if not ids:
        return 0, 0

    previous_ids = set(_seen_tweet_ids)
    if not _timeline_bootstrapped:
        _timeline_bootstrapped = True
        fresh_cutoff = (_stream_started_at or time.time()) - BOOTSTRAP_RECENCY_GRACE_SEC
        new_ids = [
            tid for tid in ids
            if tid not in previous_ids and (_tweet_id_created_unix(tid) or 0) >= fresh_cutoff
        ]
        _seen_tweet_ids.update(ids)
    else:
        new_ids = [tid for tid in ids if tid not in _seen_tweet_ids]
        if new_ids:
            _seen_tweet_ids.update(new_ids)

    # Poll-based fallbacks (search/notifications) may observe only "old" tweets
    # on first load. Seed a synthetic anchor count from the visible set so that
    # future increments can still fire instantly on truly new IDs.
    is_poll_source = source in ("search_timeline_poll", "notifications_poll")
    if not new_ids:
        if is_poll_source:
            current = get_live_count()
            if current is None:
                baseline = max(0, len(_seen_tweet_ids) or len(ids))
                set_live_count(baseline, source=f"{source}_snapshot")
            else:
                # Keep heartbeat/count freshness while preserving same level.
                set_live_count(int(current), source=source)
        return len(ids), 0

    current = get_live_count()
    if current is None:
        seeded = _seed_count_from_baseline_file()
        if seeded is not None:
            current = seeded
    if current is None:
        current = 0

    old = set_live_count(current + len(new_ids), source=source)
    newest_id = max(new_ids, key=lambda x: int(x))
    m = meta.get(newest_id, {})
    mark_signal_event(
        tweet_id=newest_id,
        created_at=m.get("created_at"),
        text=m.get("full_text"),
    )
    for tid in new_ids:
        tm = meta.get(tid, {})
        _append_signal_event(
            tid,
            source,
            created_at=tm.get("created_at"),
            full_text=tm.get("full_text"),
        )
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    lat_ms = get_last_signal_latency_ms()
    lat_txt = f"{lat_ms:.1f}ms" if lat_ms is not None else "n/a"
    _p(f"\n{'='*55}")
    _p(f"  🚨 TWEET DETECTED (original tweet)")
    if watch_username:
        _p(f"  User:    @{watch_username}")
    _p(f"  Source:  {source}")
    _p(f"  Time:    {ts}")
    _p(f"  Count:   {old:,} -> {current + len(new_ids):,} (+{len(new_ids)})")
    _p(f"  Delay:   {lat_txt} tweet->signal")
    _p(f"{'='*55}\n")
    return len(ids), len(new_ids)


def _append_signal_event(tweet_id, source, created_at=None, full_text=None):
    try:
        detected_unix = time.time()
        snowflake_unix = _tweet_id_created_unix(tweet_id)
        snowflake_iso = _tweet_id_created_iso(tweet_id)
        precise_latency_ms = _compute_tweet_latency_ms(tweet_id, created_at, detected_unix)
        evt = {
            "detected_at": datetime.utcnow().isoformat() + "Z",
            "detected_at_unix": detected_unix,
            "tweet_id": str(tweet_id),
            "source": source,
            "role": ROLE,
            "tweet_created_at": created_at,
            "tweet_snowflake_created_at": snowflake_iso,
            "tweet_snowflake_created_unix": snowflake_unix,
            "tweet_text": full_text,
            "tweet_id_to_detect_ms": precise_latency_ms,
            "tweet_to_detect_ms": precise_latency_ms,
        }
        with open(SIGNAL_LOG_FILE, "a") as f:
            f.write(json.dumps(evt) + "\n")
        _maybe_trim_jsonl(SIGNAL_LOG_FILE)
    except Exception:
        pass


def _append_sse_event(event_type="frame", topic=None):
    try:
        evt = {
            "time": datetime.now(timezone.utc).isoformat(),
            "unix": time.time(),
            "event_type": event_type,
            "topic": topic,
            "role": ROLE,
        }
        with open(SSE_EVENT_LOG_FILE, "a") as f:
            f.write(json.dumps(evt) + "\n")
        _maybe_trim_jsonl(SSE_EVENT_LOG_FILE)
    except Exception:
        pass


async def subscribe_to_timeline(page, sid, user_id, bearer_token):
    global _last_subscription_status, _last_subscription_at, _last_subscription_user_id
    global _last_subscription_body
    try:
        result = await page.evaluate(f"""
            (async () => {{
                const ct0 = document.cookie.split('; ')
                    .find(r=>r.startsWith('ct0='))?.split('=')[1]||'';
                const url = 'https://api.x.com/1.1/live_pipeline/update_subscriptions';
                const headers = {{
                    'authorization': '{bearer_token}',
                    'content-type': 'application/x-www-form-urlencoded',
                    'livepipeline-session': '{sid}',
                    'x-csrf-token': ct0,
                    'x-twitter-active-user': 'yes',
                    'x-twitter-auth-type': 'OAuth2Session',
                    'origin': 'https://x.com',
                    'referer': 'https://x.com/',
                }};
                const bodies = [
                    'sub_topics=user_timeline%3A{user_id}&unsub_topics=',
                    'subprotocol=updater-v2&sub=user_timeline%3A{user_id}&unsub='
                ];
                const out = [];
                for (const body of bodies) {{
                    const r = await fetch(url, {{
                        method: 'POST',
                        headers,
                        body,
                        credentials: 'include'
                    }});
                    let text = '';
                    try {{ text = await r.text(); }} catch (e) {{}}
                    out.push({{status: r.status, body, text: text.slice(0, 120)}});
                }}
                return out;
            }})()
        """)
        ok = [r for r in result if r.get("status") == 200]
        status = 200 if ok else (result[-1].get("status") if result else None)
        with _live_count_lock:
            _last_subscription_status = status
            _last_subscription_at = time.time()
            _last_subscription_user_id = str(user_id)
            _last_subscription_body = "; ".join(
                f"{r.get('status')}:{r.get('body')}" for r in result
            )
        _append_sse_event(event_type="subscription", topic=f"user_timeline:{user_id}:{status}:{_last_subscription_body}")
        _p(f"✅ Subscribed to user_timeline:{user_id} (status={status})")
        if status == 200:
            _p("🎯 SSE subscription POST accepted — waiting for tweet-topic verification")
    except Exception as e:
        _p(f"⚠️  Subscribe error: {e}")


async def search_timeline_poll_loop(context, username, interval_ms):
    global _last_search_poll_status, _last_search_poll_at
    global _last_search_poll_error, _last_search_poll_tweet_count

    page = await context.new_page()
    search_url = (
        "https://x.com/search?q="
        + quote(f"from:{username}")
        + "&src=typed_query&f=live"
    )
    _p(f"🔎 SearchTimeline fallback polling every {interval_ms}ms")
    first_load = True

    while True:
        try:
            async with page.expect_response(
                lambda r: "SearchTimeline" in r.url,
                timeout=max(20_000, int(interval_ms * 10)),
            ) as resp_info:
                if first_load:
                    await page.goto(
                        search_url,
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                    first_load = False
                else:
                    await page.reload(wait_until="domcontentloaded", timeout=45_000)
            resp = await resp_info.value
            status = resp.status
            text = await resp.text()

            if status == 200 and text:
                obj = json.loads(text)
                total, new = _process_countable_tweets_from_obj(
                    obj,
                    "search_timeline_poll",
                    watch_username=username,
                )
                with _live_count_lock:
                    _last_search_poll_status = status
                    _last_search_poll_at = time.time()
                    _last_search_poll_error = None
                    _last_search_poll_tweet_count = total
                _append_sse_event(
                    event_type="search_poll",
                    topic=f"search:{username}:tweets={total}:new={new}",
                )
            else:
                with _live_count_lock:
                    _last_search_poll_status = status
                    _last_search_poll_at = time.time()
                    _last_search_poll_error = text[:160] if text else None
                _append_sse_event(
                    event_type="search_poll_status",
                    topic=f"search:{username}:status={status}",
                )
        except Exception as e:
            with _live_count_lock:
                _last_search_poll_status = "error"
                _last_search_poll_at = time.time()
                _last_search_poll_error = str(e)[:160]
            _append_sse_event(
                event_type="search_poll_status",
                topic=f"search:{username}:error",
            )

        await asyncio.sleep(max(0.25, interval_ms / 1000.0))


async def notifications_poll_loop(context, username, interval_ms):
    global _last_notifications_poll_status, _last_notifications_poll_at
    global _last_notifications_poll_error, _last_notifications_poll_tweet_count

    page = await context.new_page()
    notifications_url = "https://x.com/notifications"
    _p(f"🔔 Notifications fallback polling every {interval_ms}ms")
    first_load = True

    while True:
        try:
            async with page.expect_response(
                lambda r: (
                    "NotificationsTimeline" in r.url
                    or "/i/api/graphql/" in r.url and "Notification" in r.url
                ),
                timeout=max(20_000, int(interval_ms * 10)),
            ) as resp_info:
                if first_load:
                    await page.goto(
                        notifications_url,
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                    first_load = False
                else:
                    await page.reload(wait_until="domcontentloaded", timeout=45_000)

            resp = await resp_info.value
            status = resp.status
            text = await resp.text()

            if status == 200 and text:
                obj = json.loads(text)
                total, new = _process_countable_tweets_from_obj(
                    obj,
                    "notifications_poll",
                    watch_username=username,
                )
                with _live_count_lock:
                    _last_notifications_poll_status = status
                    _last_notifications_poll_at = time.time()
                    _last_notifications_poll_error = None
                    _last_notifications_poll_tweet_count = total
                _append_sse_event(
                    event_type="notifications_poll",
                    topic=f"notifications:{username}:tweets={total}:new={new}",
                )
            else:
                with _live_count_lock:
                    _last_notifications_poll_status = status
                    _last_notifications_poll_at = time.time()
                    _last_notifications_poll_error = text[:160] if text else None
                _append_sse_event(
                    event_type="notifications_poll_status",
                    topic=f"notifications:{username}:status={status}",
                )
        except Exception as e:
            with _live_count_lock:
                _last_notifications_poll_status = "error"
                _last_notifications_poll_at = time.time()
                _last_notifications_poll_error = str(e)[:160]
            _append_sse_event(
                event_type="notifications_poll_status",
                topic=f"notifications:{username}:error",
            )

        await asyncio.sleep(max(0.25, interval_ms / 1000.0))


async def run_browser(cookie_string, bearer_token, csrf_token,
                      username=None, user_id=None):
    global _browser_ready

    watch_username = username or ELON_USERNAME
    watch_user_id  = str(user_id or ELON_USER_ID)
    configure_watch(watch_username, watch_user_id)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-features=ServiceWorker",  # force all fetches through page
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        for c in parse_cookie_string(cookie_string):
            try:
                await context.add_cookies([c])
            except Exception:
                pass

        page = await context.new_page()

        # Unregister any service workers that bypass our fetch intercept
        await context.add_init_script("""
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.getRegistrations().then(regs => {
                    regs.forEach(r => r.unregister());
                });
            }
        """)

        # ── EXPOSE PYTHON FUNCTIONS TO JS ──
        subscribed = {"done": False}

        def on_sse_data(raw):
            global _browser_ready, _timeline_bootstrapped, _last_active_subscriptions
            try:
                _browser_ready = True
                obj   = json.loads(raw)
                topic = obj.get("topic", "")
                mark_sse_event(topic=topic)

                if topic == "/system/subscriptions":
                    payload = obj.get("payload", {})
                    with _live_count_lock:
                        _last_active_subscriptions = json.dumps(payload, separators=(",", ":"))[:500]

                # Extract session ID from system config
                if not subscribed["done"] and topic == "/system/config":
                    sid = (obj.get("payload", {})
                              .get("config", {})
                              .get("session_id"))
                    if sid:
                        _p(f"🔑 Session ID from SSE: {sid}")
                        subscribed["done"] = True
                        asyncio.ensure_future(
                            subscribe_to_timeline(page, sid, watch_user_id, bearer_token)
                        )

                # Count only original tweets from timeline payload IDs.
                # This avoids counting replies/engagement events as tweets.
                if "user_timeline" in topic:
                    _process_countable_tweets_from_obj(
                        obj,
                        "sse_user_timeline",
                        watch_username=watch_username,
                    )
                # Ignore non-count topics to keep output clean
            except Exception as e:
                _p(f"SSE parse error: {e} | {raw[:80]}")

        def on_search_poll_data(raw):
            global _browser_ready
            global _last_search_poll_status, _last_search_poll_at
            global _last_search_poll_error, _last_search_poll_tweet_count
            try:
                _browser_ready = True
                obj = json.loads(raw)
                total, new = _process_countable_tweets_from_obj(
                    obj,
                    "search_timeline_poll",
                    watch_username=watch_username,
                )
                with _live_count_lock:
                    _last_search_poll_status = 200
                    _last_search_poll_at = time.time()
                    _last_search_poll_error = None
                    _last_search_poll_tweet_count = total
                _append_sse_event(
                    event_type="search_poll",
                    topic=f"search:{watch_username}:tweets={total}:new={new}",
                )
            except Exception as e:
                with _live_count_lock:
                    _last_search_poll_status = "parse_error"
                    _last_search_poll_at = time.time()
                    _last_search_poll_error = str(e)[:160]

        def on_search_poll_status(status, msg):
            global _last_search_poll_status, _last_search_poll_at, _last_search_poll_error
            with _live_count_lock:
                _last_search_poll_status = status
                _last_search_poll_at = time.time()
                _last_search_poll_error = msg[:160] if msg else None
            _append_sse_event(
                event_type="search_poll_status",
                topic=f"search:{watch_username}:status={status}",
            )

        def on_sse_connected(url):
            global _browser_ready
            _browser_ready = True
            _append_sse_event(event_type="connected", topic=url)
            _p(f"📡 SSE intercepted: {url[:100]}")

        def on_sse_error(msg):
            _p(f"⚠️  SSE error: {msg}")

        await page.expose_function("pySSEData",      on_sse_data)
        await page.expose_function("pySSEConnected", on_sse_connected)
        await page.expose_function("pySSEError",     on_sse_error)
        await page.expose_function("pySearchPollData", on_search_poll_data)
        await page.expose_function("pySearchPollStatus", on_search_poll_status)

        # ── JS FETCH INTERCEPTION ──
        await page.add_init_script("""
            (function() {
                const _orig = window.fetch;
                window.fetch = async function(...args) {
                    const url = (args[0] && args[0].url) ? args[0].url : args[0];
                    const resp = await _orig.apply(this, args);

                    if (typeof url === 'string' && url.includes('live_pipeline/events')) {
                        window.pySSEConnected(url);
                        const clone  = resp.clone();
                        const reader = clone.body.getReader();
                        const dec    = new TextDecoder();
                        (async () => {
                            let buf = '';
                            try {
                                while (true) {
                                    const {done, value} = await reader.read();
                                    if (done) break;
                                    buf += dec.decode(value, {stream: true});
                                    const parts = buf.split('\\n\\n');
                                    buf = parts.pop();
                                    for (const block of parts) {
                                        for (const line of block.split('\\n')) {
                                            if (line.startsWith('data:')) {
                                                const d = line.slice(5).trim();
                                                if (d && d !== 'ping') {
                                                    window.pySSEData(d);
                                                }
                                            }
                                        }
                                    }
                                }
                            } catch(e) {
                                window.pySSEError(e.message);
                            }
                        })();
                    }
                    return resp;
                };
            })();
        """)

        # ── NAVIGATE ──
        _p(f"🌐 Opening x.com/{watch_username}...")
        await page.goto(
            f"https://x.com/{watch_username}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        _p("✅ Page loaded")

        # ── FORCE DIRECT LIVE PIPELINE STREAM ──
        # Do not depend on website internals to open SSE.
        try:
            await page.evaluate(f"""
                () => {{
                    if (window.__polybotSSEStarted) return;
                    window.__polybotSSEStarted = true;
                    const ct0 = document.cookie.split('; ')
                        .find(r=>r.startsWith('ct0='))?.split('=')[1] || '';
                    const headers = {{
                        'authorization': '{bearer_token}',
                        'x-csrf-token': ct0,
                        'x-twitter-active-user': 'yes',
                        'x-twitter-auth-type': 'OAuth2Session',
                        'accept': 'text/event-stream',
                        'cache-control': 'no-cache',
                        'referer': 'https://x.com/',
                    }};
                    const url = 'https://api.x.com/live_pipeline/events?topic=%2Fsystem%2Fconfig';
                    (async () => {{
                        try {{
                            const resp = await fetch(url, {{
                                method: 'GET',
                                credentials: 'include',
                                headers
                            }});
                            window.pySSEConnected(url);
                            if (!resp.ok || !resp.body) {{
                                window.pySSEError('live_pipeline status=' + resp.status);
                                return;
                            }}
                            const reader = resp.body.getReader();
                            const dec = new TextDecoder();
                            let buf = '';
                            while (true) {{
                                const {{done, value}} = await reader.read();
                                if (done) break;
                                buf += dec.decode(value, {{stream: true}});
                                const parts = buf.split('\\n\\n');
                                buf = parts.pop();
                                for (const block of parts) {{
                                    for (const line of block.split('\\n')) {{
                                        if (!line.startsWith('data:')) continue;
                                        const d = line.slice(5).trim();
                                        if (d && d !== 'ping') window.pySSEData(d);
                                    }}
                                }}
                            }}
                            window.pySSEError('live_pipeline stream closed');
                        }} catch (e) {{
                            window.pySSEError('live_pipeline exception: ' + e.message);
                        }}
                    }})();
                }}
            """)
            _p("🚀 Direct live_pipeline stream requested")
        except Exception as e:
            _p(f"⚠️  Could not start direct live stream: {e}")

        if ENABLE_SEARCH_POLL:
            asyncio.create_task(
                search_timeline_poll_loop(
                    context,
                    watch_username,
                    max(250, SEARCH_POLL_MS),
                )
            )
        if ENABLE_NOTIFICATIONS_POLL:
            asyncio.create_task(
                notifications_poll_loop(
                    context,
                    watch_username,
                    max(500, NOTIFICATIONS_POLL_MS),
                )
            )

        # ── GRAPHQL FALLBACK ──
        _p("📊 GraphQL fallback polling every 5s...")
        stale_warned = False
        while True:
            await asyncio.sleep(5)
            try:
                count = None
                count_source = get_count_source()
                hb = get_last_event_update()
                sse_streaming = hb is not None and (time.time() - hb) < 45.0
                if get_live_count() is None or count_source == "seed" or not sse_streaming:
                    count = await page.evaluate(f"""
                        async () => {{
                            try {{
                                const ct0 = document.cookie.split('; ').find(r=>r.startsWith('ct0='))?.split('=')[1]||'';
                                const r = await fetch(
                                    'https://x.com/i/api/graphql/Y59DTUMfcKmUAATiT2SlTw/UserTweets?variables=%7B%22userId%22%3A%22{watch_user_id}%22%2C%22count%22%3A1%2C%22includePromotedContent%22%3Atrue%2C%22withQuickPromoteEligibilityTweetFields%22%3Atrue%2C%22withVoice%22%3Atrue%7D&features=%7B%22rweb_video_screen_enabled%22%3Afalse%2C%22responsive_web_graphql_timeline_navigation_enabled%22%3Atrue%2C%22communities_web_enable_tweet_community_results_fetch%22%3Atrue%2C%22articles_preview_enabled%22%3Atrue%2C%22responsive_web_edit_tweet_api_enabled%22%3Atrue%2C%22view_counts_everywhere_api_enabled%22%3Atrue%2C%22longform_notetweets_consumption_enabled%22%3Atrue%2C%22tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled%22%3Atrue%2C%22longform_notetweets_rich_text_read_enabled%22%3Atrue%7D&fieldToggles=%7B%22withArticlePlainText%22%3Afalse%7D',
                                    {{
                                        credentials: 'include',
                                        headers: {{
                                            'authorization': '{bearer_token}',
                                            'x-csrf-token': ct0,
                                            'x-twitter-active-user': 'yes',
                                            'x-twitter-auth-type': 'OAuth2Session',
                                        }}
                                    }}
                                );
                                const d = await r.json();
                                const find = o => {{
                                    if (!o || typeof o !== 'object') return null;
                                    if (typeof o.statuses_count === 'number') return o.statuses_count;
                                    for (const v of Object.values(o)) {{
                                        const rr = find(v);
                                        if (rr !== null) return rr;
                                    }}
                                    return null;
                                }};
                                return find(d);
                            }} catch(e) {{ return null; }}
                        }}
                    """)
                if count is not None:
                    set_live_count(count, source="graphql_user_tweets")
                    _browser_ready = True
                    ts = datetime.now().strftime("%H:%M:%S")
                    _p(f"[{ts}] ✅ GraphQL refreshed count: {count:,}")
            except Exception:
                pass

            # If no SSE data frames arrive for too long, restart browser loop.
            # This keeps the stream fresh overnight.
            hb = get_last_event_update()
            if hb:
                age = time.time() - hb
                if age > 180 and not stale_warned:
                    stale_warned = True
                    _p(f"⚠️  SSE quiet for {age:.0f}s — will restart stream only if this persists")
                if age > 300:
                    raise RuntimeError(f"SSE stale for {age:.0f}s")
            else:
                stale_warned = False
            try:
                await page.evaluate("window.scrollBy(0,1);window.scrollBy(0,-1)")
            except Exception:
                break


def parse_cookie_string(cookie_str):
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append({
            "name": name, "value": value.strip(),
            "domain": ".x.com", "path": "/", "secure": True,
        })
    return cookies


def find_statuses_count(obj):
    if isinstance(obj, dict):
        if "statuses_count" in obj:
            return obj["statuses_count"]
        for v in obj.values():
            r = find_statuses_count(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = find_statuses_count(item)
            if r is not None:
                return r
    return None


def append_bot_reaction_event(bot, weekly_before, weekly_after, lag_ms, count_source=None):
    try:
        evt = {
            "time": datetime.now(timezone.utc).isoformat(),
            "bot": bot,
            "role": ROLE,
            "weekly_before": int(weekly_before),
            "weekly_after": int(weekly_after),
            "delta": int(weekly_after) - int(weekly_before),
            "lag_ms": round(float(lag_ms), 1) if lag_ms is not None else None,
            "count_source": count_source,
        }
        with open(REACTION_LOG_FILE, "a") as f:
            f.write(json.dumps(evt) + "\n")
    except Exception:
        pass


def start_sse_browser():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    cookie = os.getenv("TWITTER_COOKIE", "")
    bearer = os.getenv("TWITTER_BEARER", "")
    csrf   = os.getenv("TWITTER_CSRF", "")
    if not cookie:
        _p("❌ No TWITTER_COOKIE in .env")
        return False

    # Seed count so timeline-ID increments have a baseline immediately.
    if get_live_count() is None:
        seeded = _seed_count_from_baseline_file()
        if seeded is not None:
            set_live_count(seeded, source="seed")
            _p(f"🔢 Seeded live tweet count: {seeded}")

    def run():
        while True:
            try:
                asyncio.run(run_browser(cookie, bearer, csrf))
            except Exception as e:
                _p(f"⚠️  Browser crashed: {e} — restarting in 10s")
            time.sleep(10)

    t = threading.Thread(target=run, daemon=True, name="SSEBrowser")
    t.start()
    _p("🌐 Browser SSE starting (headless Chromium)...")
    return True


def get_tweet_count_sse():
    return get_live_count()


def is_ready():
    return _browser_ready and _live_count is not None and _last_count_source != "seed"


if __name__ == "__main__":
    TEST_USERNAME = "Valionit"
    TEST_USER_ID  = "1856108166670323712"

    _p("🧪 Testing Live Pipeline SSE")
    _p(f"   Watching: @{TEST_USERNAME}")
    _p("   Tweet something to test detection!")
    _p("   Ctrl+C to stop\n")

    import os
    from dotenv import load_dotenv
    load_dotenv()
    cookie = os.getenv("TWITTER_COOKIE", "")
    bearer = os.getenv("TWITTER_BEARER", "")
    csrf   = os.getenv("TWITTER_CSRF", "")

    def run():
        asyncio.run(run_browser(cookie, bearer, csrf,
                                username=TEST_USERNAME,
                                user_id=TEST_USER_ID))

    t = threading.Thread(target=run, daemon=True)
    t.start()

    _p("Waiting for browser to connect...")
    for i in range(60):
        time.sleep(2)
        count = get_live_count()
        if count:
            _p(f"✅ Connected! Count: {count:,}")
            break
        _p(f"   {i*2}s...", end="\r")
    else:
        _p("\n⚠️  No count yet — tweet something!")

    _p("\nListening... tweet now! Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _p("\nDone")
