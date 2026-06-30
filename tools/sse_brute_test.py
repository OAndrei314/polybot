"""Brute-force SSE smoke test — try every authentication / topic angle.

For each variant, opens a connection, lets it run for `--per-variant-s` seconds,
counts received frames, classifies the failure mode if any, then moves on.
At the end prints a leaderboard of which variants delivered frames.

Variants tested:
  V1  cookie + bearer + POST subscribe (current production path)
  V2  cookie + bearer + GET-only with topic in URL (no separate POST)
  V3  guest token (no cookie) + GET with topic in URL
  V4  guest token (no cookie) + POST subscribe
  V5  every other account from accounts.json with V1
  V6  cookie + bearer + home_timeline topic instead of user_timeline
  V7  cookie + bearer + tweet_engagement topic (long shot)
  V8  cookie + bearer + POST subscribe to ELON's user id directly (not auth uid)

Run from repo root:
    .\.venv\Scripts\python.exe tools\sse_brute_test.py
    .\.venv\Scripts\python.exe tools\sse_brute_test.py --per-variant-s 25
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

try:
    from curl_cffi import requests as cffi_requests  # noqa: E402
    _IMPERSONATE = "chrome"
except ImportError:
    cffi_requests = None
    _IMPERSONATE = None

ELON_USER_ID = "44196397"
SSE_URL = "https://api.x.com/live_pipeline/events"
SUBSCRIBE_URL = "https://api.x.com/1.1/live_pipeline/update_subscriptions"
GUEST_ACTIVATE_URL = "https://api.x.com/1.1/guest/activate.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

DEFAULT_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


def _ct0(cookie: str) -> str:
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith("ct0="):
            return part.split("=", 1)[1]
    return ""


def _twid(cookie: str) -> str:
    for part in (cookie or "").split(";"):
        part = part.strip()
        if part.startswith("twid="):
            v = urllib.parse.unquote(part.split("=", 1)[1])
            if v.startswith("u="):
                return v[2:]
    return ""


def _new_session():
    if _IMPERSONATE and cffi_requests is not None:
        return cffi_requests.Session(impersonate=_IMPERSONATE)
    return requests.Session()


def _headers_cookie(bearer: str, cookie: str, csrf: str) -> dict:
    return {
        "authorization": bearer,
        "x-csrf-token": csrf or _ct0(cookie),
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://x.com",
        "referer": "https://x.com/home",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": UA,
        "cookie": cookie,
    }


def _headers_guest(bearer: str, guest_token: str) -> dict:
    return {
        "authorization": bearer,
        "x-guest-token": guest_token,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://x.com",
        "referer": "https://x.com/home",
        "user-agent": UA,
    }


def _get_guest_token(bearer: str) -> str | None:
    sess = _new_session()
    try:
        r = sess.post(
            GUEST_ACTIVATE_URL,
            headers={
                "authorization": bearer,
                "user-agent": UA,
                "accept": "*/*",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return (r.json() or {}).get("guest_token")
        print(f"    guest token activate: status={r.status_code} body={(r.text or '')[:200]}")
    except Exception as e:
        print(f"    guest token activate error: {e}")
    return None


class StreamProbe:
    """Open an SSE GET, optionally POST a subscribe, count frames for N seconds."""

    def __init__(self, name: str, get_url: str, get_headers: dict,
                 subscribe: tuple[str, dict, str] | None = None):
        self.name = name
        self.get_url = get_url
        self.get_headers = get_headers
        self.subscribe = subscribe  # (url, headers, body) or None
        self.frames: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.subscribe_status: int | None = None
        self.subscribe_body: str | None = None
        self.session_id: str | None = None
        self.connect_status: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _do_subscribe(self) -> None:
        if not self.subscribe:
            return
        url, headers, body = self.subscribe
        sess = _new_session()
        try:
            r = sess.post(url, headers=headers, data=body, timeout=10)
            self.subscribe_status = r.status_code
            self.subscribe_body = (r.text or "")[:300]
        except Exception as e:
            self.errors.append(f"subscribe error: {e}")

    def _run(self) -> None:
        try:
            sess = _new_session()
            r = sess.get(
                self.get_url,
                headers=self.get_headers,
                stream=True,
                timeout=(10, 0) if _IMPERSONATE else (10, None),
            )
            self.connect_status = r.status_code
            if r.status_code != 200:
                self.errors.append(f"GET status {r.status_code}: {(r.text or '')[:200]}")
                return
            sub_started = False
            for raw in r.iter_lines():
                if self._stop.is_set():
                    break
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "ping":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                topic = obj.get("topic", "")
                self.frames.append({"ts": time.time(), "topic": topic, "payload_len": len(data)})
                # capture session id and trigger subscribe after first frame
                payload = obj.get("payload") or {}
                if not sub_started:
                    cfg = payload.get("config") or {}
                    sid = cfg.get("session_id")
                    if sid:
                        self.session_id = sid
                        if self.subscribe:
                            # rewrite headers to include the just-issued session id
                            self.subscribe = (
                                self.subscribe[0],
                                {**self.subscribe[1], "livepipeline-session": sid},
                                self.subscribe[2],
                            )
                        sub_started = True
                        threading.Thread(target=self._do_subscribe, daemon=True).start()
        except Exception as e:
            self.errors.append(f"GET error: {e}")

    def report(self) -> dict:
        topics: dict[str, int] = {}
        for fr in self.frames:
            topics[fr["topic"]] = topics.get(fr["topic"], 0) + 1
        # frames that look like real timeline data (not /system/*)
        timeline = sum(c for t, c in topics.items() if "user_timeline" in t or "home_timeline" in t)
        return {
            "name": self.name,
            "connect_status": self.connect_status,
            "session_id_seen": bool(self.session_id),
            "subscribe_status": self.subscribe_status,
            "subscribe_body": self.subscribe_body,
            "frames_total": len(self.frames),
            "frames_timeline": timeline,
            "frames_by_topic": topics,
            "errors": self.errors,
        }


def variant_cookie_post(label: str, bearer: str, cookie: str, csrf: str,
                         topic_user_id: str, sub_user_id: str) -> StreamProbe:
    """V1-style: GET /system/config, then POST subscribe with sub_topics=user_timeline:UID."""
    headers = _headers_cookie(bearer, cookie, csrf)
    body = f"sub_topics=user_timeline%3A{sub_user_id}&unsub_topics="
    return StreamProbe(
        name=label,
        get_url=SSE_URL + "?topic=%2Fsystem%2Fconfig",
        get_headers={**headers, "accept": "text/event-stream", "cache-control": "no-cache"},
        subscribe=(SUBSCRIBE_URL, {**headers, "content-type": "application/x-www-form-urlencoded"}, body),
    )


def variant_cookie_get_only(label: str, bearer: str, cookie: str, csrf: str,
                              topic: str) -> StreamProbe:
    """GET with topic= in URL, no POST subscribe."""
    headers = _headers_cookie(bearer, cookie, csrf)
    return StreamProbe(
        name=label,
        get_url=SSE_URL + "?topic=" + urllib.parse.quote(topic, safe=""),
        get_headers={**headers, "accept": "text/event-stream", "cache-control": "no-cache"},
        subscribe=None,
    )


def variant_guest_get_only(label: str, bearer: str, guest_token: str, topic: str) -> StreamProbe:
    headers = _headers_guest(bearer, guest_token)
    return StreamProbe(
        name=label,
        get_url=SSE_URL + "?topic=" + urllib.parse.quote(topic, safe=""),
        get_headers={**headers, "accept": "text/event-stream", "cache-control": "no-cache"},
        subscribe=None,
    )


def variant_guest_post(label: str, bearer: str, guest_token: str, sub_user_id: str) -> StreamProbe:
    headers = _headers_guest(bearer, guest_token)
    body = f"sub_topics=user_timeline%3A{sub_user_id}&unsub_topics="
    return StreamProbe(
        name=label,
        get_url=SSE_URL + "?topic=%2Fsystem%2Fconfig",
        get_headers={**headers, "accept": "text/event-stream", "cache-control": "no-cache"},
        subscribe=(SUBSCRIBE_URL, {**headers, "content-type": "application/x-www-form-urlencoded"}, body),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="accounts.json")
    ap.add_argument("--per-variant-s", type=float, default=18.0)
    ap.add_argument("--bearer", default=None,
                    help="override bearer; defaults to per-account or DEFAULT_BEARER")
    args = ap.parse_args()

    with open(args.accounts) as f:
        accounts = json.load(f)
    if not accounts:
        print("no accounts in accounts.json", file=sys.stderr)
        return 2

    primary = accounts[0]
    primary_bearer = args.bearer or primary["bearer"]
    primary_cookie = primary["cookie"]
    primary_csrf = primary.get("csrf", "")
    primary_uid = _twid(primary_cookie) or ELON_USER_ID

    print(f"=== brute SSE test ({len(accounts)} accounts available)")
    print(f"  primary auth uid: {primary_uid}")
    print(f"  curl_cffi available: {bool(_IMPERSONATE)}")

    # Build the variant battery
    probes: list[StreamProbe] = []

    probes.append(variant_cookie_post(
        "V1_cookie_POST_user_timeline_authuid",
        primary_bearer, primary_cookie, primary_csrf,
        topic_user_id=primary_uid, sub_user_id=primary_uid,
    ))

    probes.append(variant_cookie_get_only(
        "V2_cookie_GET_only_user_timeline",
        primary_bearer, primary_cookie, primary_csrf,
        topic=f"user_timeline:{primary_uid}",
    ))

    probes.append(variant_cookie_get_only(
        "V6_cookie_GET_only_home_timeline",
        primary_bearer, primary_cookie, primary_csrf,
        topic=f"home_timeline:{primary_uid}",
    ))

    probes.append(variant_cookie_get_only(
        "V7_cookie_GET_only_tweet_engagement_elon",
        primary_bearer, primary_cookie, primary_csrf,
        topic=f"tweet_engagement:{ELON_USER_ID}",
    ))

    probes.append(variant_cookie_post(
        "V8_cookie_POST_user_timeline_ELON_uid",
        primary_bearer, primary_cookie, primary_csrf,
        topic_user_id=primary_uid, sub_user_id=ELON_USER_ID,
    ))

    # Guest-token variants
    print("  fetching guest token...", flush=True)
    gt = _get_guest_token(primary_bearer)
    if gt:
        print(f"  guest_token = {gt[:18]}...{gt[-4:] if len(gt) > 22 else gt}")
        probes.append(variant_guest_get_only(
            "V3_guest_GET_only_user_timeline_ELON",
            primary_bearer, gt, f"user_timeline:{ELON_USER_ID}",
        ))
        probes.append(variant_guest_post(
            "V4_guest_POST_user_timeline_ELON",
            primary_bearer, gt, ELON_USER_ID,
        ))
    else:
        print("  guest token unavailable — skipping V3/V4")

    # V5: try V1 with each remaining account
    for acc in accounts[1:]:
        lbl = acc.get("label", "?")
        uid = _twid(acc["cookie"]) or ELON_USER_ID
        probes.append(variant_cookie_post(
            f"V5_{lbl}_cookie_POST_user_timeline",
            acc["bearer"], acc["cookie"], acc.get("csrf", ""),
            topic_user_id=uid, sub_user_id=uid,
        ))

    # Run probes sequentially
    results: list[dict] = []
    for i, probe in enumerate(probes, 1):
        print()
        print(f"[{i}/{len(probes)}] === {probe.name} ===", flush=True)
        probe.start()
        time.sleep(args.per_variant_s)
        probe.stop()
        time.sleep(0.5)
        rep = probe.report()
        results.append(rep)
        print(f"   connect_status={rep['connect_status']} session_id={rep['session_id_seen']} "
              f"subscribe_status={rep['subscribe_status']} frames_total={rep['frames_total']} "
              f"frames_timeline={rep['frames_timeline']}")
        if rep['frames_by_topic']:
            print(f"   topics: {rep['frames_by_topic']}")
        if rep['subscribe_body']:
            print(f"   subscribe_body: {rep['subscribe_body']}")
        if rep['errors']:
            print(f"   errors: {rep['errors']}")

    # Final leaderboard
    print()
    print("=== LEADERBOARD (sorted by frames_timeline desc) ===")
    results.sort(key=lambda r: (-r['frames_timeline'], -r['frames_total'], r['name']))
    print(f"{'NAME':45s} {'CONN':>4s} {'SUB':>4s} {'TOTAL':>6s} {'TIMELINE':>9s}")
    for r in results:
        cs = r['connect_status'] if r['connect_status'] is not None else '-'
        ss = r['subscribe_status'] if r['subscribe_status'] is not None else '-'
        print(f"{r['name']:45s} {str(cs):>4s} {str(ss):>4s} {r['frames_total']:>6d} {r['frames_timeline']:>9d}")

    winners = [r for r in results if r['frames_timeline'] > 0]
    print()
    if winners:
        print(f"WIN: {len(winners)} variant(s) delivered timeline frames!")
        for r in winners:
            print(f"   - {r['name']}: {r['frames_timeline']} timeline frames")
    else:
        print("FAIL: no variant delivered timeline frames.")
        any_session = sum(1 for r in results if r['session_id_seen'])
        any_connect = sum(1 for r in results if r['connect_status'] == 200)
        print(f"   {any_connect}/{len(results)} got 200 on GET")
        print(f"   {any_session}/{len(results)} received session_id frames")
        sub_codes = set(r['subscribe_status'] for r in results if r['subscribe_status'] is not None)
        print(f"   subscribe statuses seen: {sorted(s for s in sub_codes if s is not None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
