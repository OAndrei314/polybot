"""Stupidest possible SSE smoke test.

Loads the first account from accounts.json, connects to X live_pipeline,
and prints every event/tweet for 60 seconds. Run from repo root:

    .\.venv\Scripts\python.exe tools\sse_dumb_test.py
    .\.venv\Scripts\python.exe tools\sse_dumb_test.py --account latenightsatire
    .\.venv\Scripts\python.exe tools\sse_dumb_test.py --duration-s 300

Prints "ALIVE", subscribe status, every frame, and a final summary.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.sse_direct import DirectSSEClient, ELON_USER_ID  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="accounts.json")
    ap.add_argument("--account", default=None, help="account label to use (default: first)")
    ap.add_argument("--duration-s", type=float, default=60.0)
    ap.add_argument("--watch-user-id", default=ELON_USER_ID)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    with open(args.accounts) as f:
        accounts = json.load(f)
    if not accounts:
        print("no accounts in accounts.json", file=sys.stderr)
        return 2

    if args.account:
        acc = next((a for a in accounts if a.get("label") == args.account), None)
        if acc is None:
            print(f"no account labeled {args.account!r}", file=sys.stderr)
            return 2
    else:
        acc = accounts[0]

    label = acc.get("label", "?")
    print(f"=== using account: {label}", flush=True)

    counts = {"frame": 0, "tweet": 0, "subscribed": 0, "other": 0}
    first_frame_at: list[float] = []
    first_tweet_at: list[float] = []
    started_at = time.time()

    def on_event(evt: dict) -> None:
        kind = str(evt.get("kind") or "other")
        counts[kind] = counts.get(kind, 0) + 1
        elapsed = time.time() - started_at
        if kind == "frame":
            if not first_frame_at:
                first_frame_at.append(elapsed)
            topic = evt.get("topic", "")
            print(f"  +{elapsed:6.2f}s  frame  topic={topic}", flush=True)
        elif kind == "subscribed":
            print(f"  +{elapsed:6.2f}s  subscribed status={evt.get('status')} ok={evt.get('ok')}", flush=True)
        else:
            print(f"  +{elapsed:6.2f}s  {kind}  {evt}", flush=True)

    def on_tweet(t: dict) -> None:
        counts["tweet"] = counts.get("tweet", 0) + 1
        elapsed = time.time() - started_at
        if not first_tweet_at:
            first_tweet_at.append(elapsed)
        tid = t.get("tweet_id") or t.get("id_str") or "?"
        text = (t.get("text") or t.get("full_text") or "")[:140].replace("\n", " ")
        print(f"  +{elapsed:6.2f}s  TWEET id={tid} text={text!r}", flush=True)

    client = DirectSSEClient(
        bearer=acc["bearer"],
        cookie=acc["cookie"],
        csrf=acc.get("csrf", ""),
        watch_user_id=args.watch_user_id,
        on_tweet=on_tweet,
        on_event=on_event,
        method_label=f"sse_test/{label}",
    )

    print(f"=== starting SSE for watch_user_id={args.watch_user_id} for {args.duration_s:.0f}s", flush=True)
    client.start()

    deadline = time.time() + args.duration_s
    while time.time() < deadline:
        time.sleep(2.0)
        st = client.state()
        elapsed = time.time() - started_at
        print(f"  +{elapsed:6.2f}s  STATE alive={st['alive']} session_id={st['session_id']} "
              f"connected_at={st['connected_at']} last_frame_at={st['last_frame_at']} "
              f"seen_tweets={st['seen']}", flush=True)

    client.stop()
    print()
    print("=== SUMMARY ===")
    print(f"  account:        {label}")
    print(f"  duration:       {args.duration_s:.0f}s")
    print(f"  frames seen:    {counts.get('frame', 0)}")
    print(f"  tweets seen:    {counts.get('tweet', 0)}")
    print(f"  subscribed:     {counts.get('subscribed', 0)}")
    print(f"  first frame at: {first_frame_at[0]:.2f}s" if first_frame_at else "  first frame at: NEVER (sse not delivering frames)")
    print(f"  first tweet at: {first_tweet_at[0]:.2f}s" if first_tweet_at else "  first tweet at: NEVER (no Elon tweets in window — could still be working)")
    print(f"  final state:    {client.state()}")
    if not first_frame_at:
        print()
        print("  → SSE is NOT delivering frames. Likely causes:")
        print("    - cookie is logged out / ct0 expired")
        print("    - X is challenging via Cloudflare on the subscribe POST")
        print("    - account has been flagged for live_pipeline access")
        print("    Try --verbose to see subscribe HTTP details.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
