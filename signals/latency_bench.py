"""
Tweet detection latency benchmark.

Runs every detection method in parallel against a single watched user
(default: elonmusk) and records every detection with a high-resolution local
timestamp so we can answer: which method saw this tweet first, and by how
many ms?

Methods running in parallel:
  - direct_sse:       pure-httpx SSE, no browser  (signals/sse_direct.py)
  - gql_user_tweets:  httpx GET to /graphql/.../UserTweets at fixed interval
  - gql_user_by_id:   httpx GET to /graphql/.../UserByRestId  (count-only)

Outputs:
  data/signal_latency/detections.jsonl   one row per (method, tweet_id) first sighting
  data/signal_latency/events.jsonl       per-method status frames (connect/error/poll)
  data/signal_latency/state.json         live aggregate snapshot, refreshed each tick

Latency model
-------------
Tweet IDs are Snowflakes. tweet_creation_unix can be recovered from the ID
itself (id >> 22 + epoch_ms). So latency_ms = (detected_at - snowflake_unix)
* 1000 is comparable across methods on the same tweet, regardless of which
machine clock the method ran on (so long as local time is NTP-synced).
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Optional
from urllib.parse import quote

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Allow running as `python -m signals.latency_bench` and as a script.
if __package__ in (None, ""):
    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE.parent))

from signals.sse_direct import DirectSSEClient, ELON_USER_ID, snowflake_unix  # noqa: E402

# ---- Endpoints ----------------------------------------------------------------

USER_TWEETS_QUERY_ID = "Y59DTUMfcKmUAATiT2SlTw"
USER_BY_REST_ID_QUERY_ID = "tD8zKvQzwY3kdx5yz6YmOw"  # may rotate; fallback to ScreenName

USER_TWEETS_FEATURES = {
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
USER_TWEETS_FIELD_TOGGLES = {"withArticlePlainText": False}


def _user_tweets_url(user_id: str, count: int = 1) -> str:
    variables = {
        "userId": str(user_id),
        "count": count,
        "includePromotedContent": True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
    }
    return (
        "https://x.com/i/api/graphql/"
        f"{USER_TWEETS_QUERY_ID}/UserTweets"
        f"?variables={quote(json.dumps(variables, separators=(',', ':')))}"
        f"&features={quote(json.dumps(USER_TWEETS_FEATURES, separators=(',', ':')))}"
        f"&fieldToggles={quote(json.dumps(USER_TWEETS_FIELD_TOGGLES, separators=(',', ':')))}"
    )


# ---- Recorder -----------------------------------------------------------------


class Recorder:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self.detections_path = out_dir / "detections.jsonl"
        self.events_path = out_dir / "events.jsonl"
        self.state_path = out_dir / "state.json"
        self._lock = threading.Lock()
        # method -> set of tweet_ids already recorded
        self._method_seen: dict[str, set[str]] = {}
        # tweet_id -> {method: detection_dict}
        self._by_tweet: dict[str, dict[str, dict]] = {}
        # method -> list of latency_ms
        self._latencies: dict[str, list[float]] = {}
        # method -> count of poll/event totals
        self._method_events: dict[str, int] = {}
        self._started_at = time.time()

    def record_detection(self, det: dict):
        method = det["method"]
        tid = det["tweet_id"]
        with self._lock:
            seen = self._method_seen.setdefault(method, set())
            if tid in seen:
                return
            seen.add(tid)
            self._by_tweet.setdefault(tid, {})[method] = det
            lat = det.get("latency_ms")
            if isinstance(lat, (int, float)):
                self._latencies.setdefault(method, []).append(float(lat))
            self._append(self.detections_path, det)

    def record_event(self, evt: dict):
        method = evt.get("method", "?")
        with self._lock:
            self._method_events[method] = self._method_events.get(method, 0) + 1
            self._append(self.events_path, evt)

    @staticmethod
    def _append(path: Path, obj: dict):
        try:
            with path.open("a") as f:
                f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        except Exception:
            pass

    def write_state(self):
        with self._lock:
            stats: dict = {}
            all_methods = set(self._method_seen) | set(self._method_events)
            for m in sorted(all_methods):
                lats = self._latencies.get(m, [])
                seen = self._method_seen.get(m, set())
                stats[m] = {
                    "detections": len(seen),
                    "events": self._method_events.get(m, 0),
                    "lat_ms_count": len(lats),
                    "lat_ms_mean": round(mean(lats), 1) if lats else None,
                    "lat_ms_p50": round(median(lats), 1) if lats else None,
                    "lat_ms_min": round(min(lats), 1) if lats else None,
                    "lat_ms_max": round(max(lats), 1) if lats else None,
                }
            # Per-tweet first-detector wins
            wins: dict[str, int] = {}
            margins_ms: dict[str, list[float]] = {}
            for tid, methods in self._by_tweet.items():
                if not methods:
                    continue
                ordered = sorted(
                    methods.items(),
                    key=lambda kv: kv[1].get("detected_at", float("inf")),
                )
                winner_m, winner_d = ordered[0]
                wins[winner_m] = wins.get(winner_m, 0) + 1
                if len(ordered) > 1:
                    runner_m, runner_d = ordered[1]
                    delta_ms = (runner_d["detected_at"] - winner_d["detected_at"]) * 1000.0
                    margins_ms.setdefault(winner_m, []).append(delta_ms)
            margin_summary: dict = {}
            for m, vals in margins_ms.items():
                if not vals:
                    continue
                margin_summary[m] = {
                    "n": len(vals),
                    "mean_ms": round(mean(vals), 1),
                    "median_ms": round(median(vals), 1),
                    "max_ms": round(max(vals), 1),
                }
            state = {
                "started_at": self._started_at,
                "uptime_s": round(time.time() - self._started_at, 1),
                "tweets_seen": len(self._by_tweet),
                "per_method": stats,
                "first_to_detect_wins": wins,
                "first_to_detect_margins_ms": margin_summary,
                "now": time.time(),
                "now_iso": datetime.now(timezone.utc).isoformat(),
            }
            try:
                self.state_path.write_text(json.dumps(state, indent=2))
            except Exception:
                pass
            return state


# ---- GraphQL pollers ----------------------------------------------------------


def _rand_hex(n: int) -> str:
    import secrets
    return secrets.token_hex(n)


def _gql_headers(bearer: str, csrf: str, cookie: str) -> dict:
    if bearer and not bearer.lower().startswith("bearer "):
        bearer = f"Bearer {bearer}"
    return {
        "authorization": bearer,
        "x-csrf-token": csrf,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "x-client-transaction-id": _rand_hex(48),
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://x.com",
        "referer": "https://x.com/",
        "cookie": cookie,
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    }


def _warmup_session(client: httpx.Client, log) -> None:
    """Hit x.com so the cookie jar / TLS session is 'warm' before GraphQL."""
    try:
        r = client.get(
            "https://x.com/elonmusk",
            headers={
                "user-agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )
        log(f"[gql_user_tweets] warmup x.com/elonmusk status={r.status_code}")
    except Exception as e:
        log(f"[gql_user_tweets] warmup error: {e}")


def _walk_extract_top_tweet_id(obj) -> Optional[tuple[str, str]]:
    """Find the most recent tweet ID + text in a UserTweets graphql payload.

    Looks for any dict with `legacy.id_str` and `legacy.full_text`. Returns
    the one with the largest numeric id (which corresponds to most recent
    Snowflake creation time).
    """
    best: Optional[tuple[int, str, str]] = None

    def visit(o):
        nonlocal best
        if isinstance(o, dict):
            legacy = o.get("legacy")
            if isinstance(legacy, dict):
                tid = legacy.get("id_str")
                if isinstance(tid, str) and tid.isdigit():
                    if legacy.get("retweeted_status_id_str") or legacy.get("in_reply_to_status_id_str"):
                        pass  # skip RT/replies
                    elif "retweeted_status_result" in o:
                        pass
                    else:
                        n = int(tid)
                        if best is None or n > best[0]:
                            best = (n, tid, legacy.get("full_text", "") or "")
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(obj)
    if best is None:
        return None
    return best[1], best[2]


def gql_user_tweets_loop(
    user_id: str,
    bearer: str,
    csrf: str,
    cookie: str,
    interval_s: float,
    recorder: Recorder,
    method_label: str,
    stop_evt: threading.Event,
    log,
):
    url = _user_tweets_url(user_id, count=1)
    headers = _gql_headers(bearer, csrf, cookie)
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    client = httpx.Client(timeout=timeout, http2=False, headers=headers, follow_redirects=False)
    seen: set[str] = set()
    backoff = interval_s
    consecutive_429 = 0
    _warmup_session(client, log)
    log(f"[{method_label}] poll every {interval_s}s -> {url[:90]}…")
    try:
        while not stop_evt.is_set():
            t0 = time.time()
            try:
                # Rotate transaction id per request to look more like a browser.
                client.headers["x-client-transaction-id"] = _rand_hex(48)
                r = client.get(url)
                ok = r.status_code == 200
                recorder.record_event({
                    "kind": "poll",
                    "status": r.status_code,
                    "elapsed_ms": round((time.time() - t0) * 1000.0, 1),
                    "method": method_label,
                    "ts": time.time(),
                })
                if ok:
                    try:
                        obj = r.json()
                    except Exception:
                        obj = None
                    if obj is not None:
                        top = _walk_extract_top_tweet_id(obj)
                        if top is not None:
                            tid, text = top
                            if tid not in seen:
                                seen.add(tid)
                                now = time.time()
                                snow = snowflake_unix(tid)
                                lat_ms = (
                                    round((now - snow) * 1000.0, 1)
                                    if snow is not None else None
                                )
                                recorder.record_detection({
                                    "tweet_id": tid,
                                    "method": method_label,
                                    "detected_at": now,
                                    "snowflake_unix": snow,
                                    "latency_ms": lat_ms,
                                    "text": (text or "")[:280],
                                })
                    backoff = interval_s
                    consecutive_429 = 0
                else:
                    body = (r.text or "")[:200]
                    log(f"[{method_label}] status={r.status_code} {body[:80]}")
                    if r.status_code == 429:
                        consecutive_429 += 1
                        backoff = min(900.0, max(15.0, backoff * 2.0))
                        if consecutive_429 >= 3:
                            _warmup_session(client, log)
                    else:
                        backoff = min(60.0, max(interval_s, backoff * 1.5))
            except Exception as e:
                recorder.record_event({
                    "kind": "error",
                    "error": str(e)[:200],
                    "method": method_label,
                    "ts": time.time(),
                })
                backoff = min(60.0, max(interval_s, backoff * 1.5))
            dt = time.time() - t0
            sleep_for = max(0.0, backoff - dt) + random.uniform(0, 0.05)
            if stop_evt.wait(sleep_for):
                break
    finally:
        client.close()


# ---- Browser observer (tails tweet_signal_events.jsonl) -----------------------


def browser_observer_loop(
    log_path: Path,
    recorder: Recorder,
    stop_evt: threading.Event,
    log,
    skip_age_s: float = 1800.0,
):
    """Tail the existing browser SSE/poll log so its detections are joined
    against direct methods on the same tweets. Each `source` becomes its own
    method label so the plot can compare them all in one frame."""
    inode = None
    pos = 0
    seen_initial = False
    while not stop_evt.is_set():
        try:
            if not log_path.exists():
                stop_evt.wait(2.0)
                continue
            st = log_path.stat()
            if inode != st.st_ino:
                inode = st.st_ino
                pos = 0
            with log_path.open("r") as f:
                if not seen_initial:
                    f.seek(0, 2)
                    pos = f.tell()
                    seen_initial = True
                    log(f"[browser_observer] tailing {log_path} from offset {pos}")
                else:
                    f.seek(pos)
                for ln in f:
                    pos = f.tell()
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = json.loads(ln)
                    except Exception:
                        continue
                    tid = obj.get("tweet_id")
                    detect_ts = obj.get("detected_at_unix")
                    source = obj.get("source") or "browser"
                    snow_ts = obj.get("tweet_snowflake_created_unix")
                    if not (isinstance(tid, str) and tid.isdigit()):
                        continue
                    if not isinstance(detect_ts, (int, float)):
                        continue
                    if isinstance(snow_ts, (int, float)) and (time.time() - snow_ts) > skip_age_s:
                        continue
                    method_label = f"browser_{source}"
                    lat_ms = obj.get("tweet_id_to_detect_ms")
                    if not isinstance(lat_ms, (int, float)) and isinstance(snow_ts, (int, float)):
                        lat_ms = round((detect_ts - snow_ts) * 1000.0, 1)
                    recorder.record_detection({
                        "tweet_id": tid,
                        "method": method_label,
                        "detected_at": float(detect_ts),
                        "snowflake_unix": float(snow_ts) if isinstance(snow_ts, (int, float)) else None,
                        "latency_ms": float(lat_ms) if isinstance(lat_ms, (int, float)) else None,
                        "text": (obj.get("tweet_text") or "")[:280],
                    })
            stop_evt.wait(0.5)
        except Exception as e:
            log(f"[browser_observer] error: {e}")
            stop_evt.wait(2.0)


# ---- Status printer -----------------------------------------------------------


def status_loop(recorder: Recorder, sse_client: DirectSSEClient,
                stop_evt: threading.Event, interval_s: float, log):
    last_print = 0.0
    while not stop_evt.is_set():
        time.sleep(0.5)
        if time.time() - last_print < interval_s:
            continue
        last_print = time.time()
        state = recorder.write_state()
        lines = []
        lines.append(
            f"\n=== latency-bench tick @ {state['now_iso']}  uptime={state['uptime_s']}s "
            f"tweets_seen={state['tweets_seen']} ==="
        )
        sse_state = sse_client.state()
        last_age = (
            f"{round(time.time() - sse_state['last_frame_at'], 1)}s"
            if sse_state.get("last_frame_at") else "—"
        )
        lines.append(
            f"direct_sse alive={sse_state['alive']} session={'yes' if sse_state['session_id'] else 'no'} "
            f"last_frame_age={last_age} seen={sse_state['seen']}"
        )
        for m, s in state["per_method"].items():
            lines.append(
                f"  {m:<22} det={s['detections']:<3} "
                f"events={s['events']:<5} "
                f"lat[mean/p50/min/max]= "
                f"{s['lat_ms_mean']}/{s['lat_ms_p50']}/{s['lat_ms_min']}/{s['lat_ms_max']} ms"
            )
        wins = state["first_to_detect_wins"]
        if wins:
            wins_str = "  ".join(f"{m}:{n}" for m, n in sorted(wins.items()))
            lines.append(f"  first-to-detect wins -> {wins_str}")
        margins = state.get("first_to_detect_margins_ms", {})
        for m, agg in margins.items():
            lines.append(
                f"  margin[{m} beats next]: n={agg['n']} median={agg['median_ms']}ms "
                f"mean={agg['mean_ms']}ms max={agg['max_ms']}ms"
            )
        log("\n".join(lines))


# ---- Main ---------------------------------------------------------------------


def main(argv=None):
    import argparse
    import logging

    p = argparse.ArgumentParser(
        prog="latency_bench",
        description="Tweet detection latency benchmark across all available methods.",
    )
    p.add_argument("--user-id", default=ELON_USER_ID, help="X user_id to watch (default: Elon).")
    p.add_argument("--out-dir", default="data/signal_latency",
                   help="Output directory for jsonl + state.json")
    p.add_argument("--gql-interval-s", type=float, default=30.0,
                   help="UserTweets GraphQL poll interval (s). Twitter's free GraphQL "
                        "rate-limit is ~150/15min; below ~6s shares-of-cookies with the "
                        "browser SSE blow through it.")
    p.add_argument("--status-s", type=float, default=15.0,
                   help="Print/refresh aggregate status every N seconds.")
    p.add_argument("--log-file", default="signal_latency.log",
                   help="Stdout/stderr mirror file (rotated by user).")
    p.add_argument("--no-gql", action="store_true",
                   help="Disable GraphQL UserTweets poller.")
    p.add_argument("--no-sse", action="store_true",
                   help="Disable direct SSE client.")
    p.add_argument("--browser-log", default="tweet_signal_events.jsonl",
                   help="Existing browser SSE/poll log to tail as a passive method.")
    p.add_argument("--no-browser-observer", action="store_true",
                   help="Disable browser_<source> observer methods.")
    args = p.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path),
        ],
    )
    log = logging.getLogger("latency_bench")

    bearer = os.getenv("TWITTER_BEARER", "")
    cookie = os.getenv("TWITTER_COOKIE", "")
    csrf = os.getenv("TWITTER_CSRF", "")
    if not bearer or not cookie:
        log.error("TWITTER_BEARER and TWITTER_COOKIE must be set in env / .env")
        return 2

    recorder = Recorder(out_dir)
    stop_evt = threading.Event()

    def _print(msg):
        log.info(msg)

    sse_client = DirectSSEClient(
        bearer=bearer,
        cookie=cookie,
        csrf=csrf,
        watch_user_id=str(args.user_id),
        on_tweet=recorder.record_detection,
        on_event=recorder.record_event,
        method_label="direct_sse",
        log=logging.getLogger("sse_direct"),
    )
    if not args.no_sse:
        sse_client.start()
        log.info("started DirectSSEClient")

    gql_thread: Optional[threading.Thread] = None
    if not args.no_gql:
        gql_thread = threading.Thread(
            target=gql_user_tweets_loop,
            kwargs=dict(
                user_id=str(args.user_id),
                bearer=bearer,
                csrf=csrf,
                cookie=cookie,
                interval_s=float(args.gql_interval_s),
                recorder=recorder,
                method_label="gql_user_tweets",
                stop_evt=stop_evt,
                log=_print,
            ),
            name="gql_user_tweets",
            daemon=True,
        )
        gql_thread.start()

    if not args.no_browser_observer:
        browser_thread = threading.Thread(
            target=browser_observer_loop,
            kwargs=dict(
                log_path=Path(args.browser_log),
                recorder=recorder,
                stop_evt=stop_evt,
                log=_print,
            ),
            name="browser_observer",
            daemon=True,
        )
        browser_thread.start()

    status_thread = threading.Thread(
        target=status_loop,
        args=(recorder, sse_client, stop_evt, float(args.status_s), _print),
        name="status",
        daemon=True,
    )
    status_thread.start()

    log.info("latency-bench running. Ctrl-C to stop.  out_dir=%s", out_dir)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("stopping…")
    finally:
        stop_evt.set()
        sse_client.stop()
        recorder.write_state()


if __name__ == "__main__":
    sys.exit(main() or 0)
