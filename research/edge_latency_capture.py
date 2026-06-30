"""
3-clock edge-latency capture.

The only candidate edge on the Elon-tweet bracket markets is *latency*: seeing a
tweet (and the resulting count change) before the Polymarket order book reprices.
This tool records the three clocks needed to settle whether that edge exists:

  t_post     true tweet creation time (recovered from the Snowflake id)
  t_detect   when OUR fast graphql path saw the tweet / count change
  t_reprice  when the affected bracket's order book first moved materially

It does NOT try to attribute online. It just logs two high-resolution streams —
detections and book-moves — to JSONL. Alignment is done offline by
`analyze_edge_latency` so we can see book moves that happened *before* we
detected (i.e. the market beating us), which an online "watch-after-detect"
loop would structurally miss.

Run:
    python -m research.edge_latency_capture --duration 1800
Then:
    python -m research.edge_latency_capture --analyze <out_dir>
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market.prices import PolymarketPrices
from signals.stream import TweetStream

# Live weekly/short Elon-tweet markets to watch. Keeping the set small keeps the
# book snapshot fast. Discovery would also work but pinning avoids pulling the
# 1000-bracket monthly market into the hot loop.
DEFAULT_SLUGS = [
    "elon-musk-of-tweets-june-2-june-9",
    "elon-musk-of-tweets-june-5-june-12",
    "elon-musk-of-tweets-june-8-june-10",
    "elon-musk-of-tweets-june-9-june-16",
    "elon-musk-of-tweets-june-11-june-13",
]

# A bracket mid must move at least this much (and stay tradeable) to count as a
# reprice event — filters out 1-tick noise on illiquid books.
MOVE_THRESHOLD = 0.01
TRADEABLE_LO, TRADEABLE_HI = 0.02, 0.98


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _mids(price_rows: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for pr in price_rows:
        slug, lab = pr.get("event_slug"), pr.get("label")
        yb, ysl = pr.get("yes_buy"), pr.get("yes_sell")
        if slug and lab and yb is not None and ysl is not None:
            out[(slug, lab)] = (yb + ysl) / 2.0
    return out


def capture(slugs: list[str], duration_s: float, out_dir: Path, book_hz: float) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    det_f = (out_dir / "detections.jsonl").open("a", buffering=1)
    mv_f = (out_dir / "bookmoves.jsonl").open("a", buffering=1)
    meta = {"started_at": _iso(_now()), "slugs": slugs, "move_threshold": MOVE_THRESHOLD}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    feed = PolymarketPrices(event_slugs=slugs)
    feed.start()
    stream = TweetStream()
    stream.start()

    print(f"[capture] warming up… out_dir={out_dir}")
    time.sleep(4)
    feed.refresh_stale_tokens(force_all=True)

    prev_mids: dict[tuple[str, str], float] = _mids(feed.snapshot())
    seen_tids: set[str] = set()
    prev_count: int | None = None
    n_det = n_move = 0
    t_end = _now() + duration_s
    book_dt = 1.0 / max(0.5, book_hz)
    next_book = 0.0

    print(f"[capture] running {duration_s:.0f}s, {len(prev_mids)} brackets tracked")
    while _now() < t_end:
        loop_t = _now()

        # --- detection stream (fast graphql) ---
        ts_state = stream.poll()
        lt = ts_state.get("latest_tweet") or {}
        tid = lt.get("tweet_id")
        if tid and tid not in seen_tids:
            seen_tids.add(tid)
            snow = lt.get("snowflake_unix")
            det = lt.get("detected_at") or loop_t
            rec = {
                "kind": "tweet",
                "tweet_id": tid,
                "t_post": snow,
                "t_detect": det,
                "detect_lag_s": (round(det - snow, 3) if snow else None),
                "source": lt.get("source"),
                "text": (lt.get("text") or "")[:120],
            }
            det_f.write(json.dumps(rec) + "\n")
            n_det += 1
            print(f"[DETECT] tid={tid} detect_lag={rec['detect_lag_s']}s src={rec['source']}")

        cnt = ts_state.get("lifetime_count")
        if isinstance(cnt, int) and cnt != prev_count:
            rec = {
                "kind": "count",
                "t_detect": loop_t,
                "lifetime_count": cnt,
                "prev": prev_count,
                "source": ts_state.get("count_source"),
                "count_age_s": ts_state.get("count_age_s"),
            }
            det_f.write(json.dumps(rec) + "\n")
            if prev_count is not None:
                print(f"[COUNT ] {prev_count} -> {cnt} src={rec['source']}")
            prev_count = cnt

        # --- book-move stream ---
        if loop_t >= next_book:
            next_book = loop_t + book_dt
            cur = _mids(feed.snapshot())
            for key, v in cur.items():
                pv = prev_mids.get(key)
                if pv is None:
                    continue
                if abs(v - pv) >= MOVE_THRESHOLD and TRADEABLE_LO < v < TRADEABLE_HI:
                    rec = {
                        "kind": "move",
                        "ts": loop_t,
                        "slug": key[0],
                        "label": key[1],
                        "old_mid": round(pv, 4),
                        "new_mid": round(v, 4),
                        "delta": round(v - pv, 4),
                    }
                    mv_f.write(json.dumps(rec) + "\n")
                    n_move += 1
            prev_mids = cur

        slept = _now() - loop_t
        time.sleep(max(0.0, 0.25 - slept))

    det_f.close()
    mv_f.close()
    feed.stop()
    stream.stop()
    print(f"[capture] done. detections={n_det} book_moves={n_move} out_dir={out_dir}")


def analyze(out_dir: Path, window_s: float = 180.0) -> None:
    import bisect
    import statistics

    dets, moves = [], []
    dp = out_dir / "detections.jsonl"
    mp = out_dir / "bookmoves.jsonl"
    if dp.exists():
        for line in dp.read_text().splitlines():
            if line.strip():
                dets.append(json.loads(line))
    if mp.exists():
        for line in mp.read_text().splitlines():
            if line.strip():
                moves.append(json.loads(line))

    move_ts = sorted(m["ts"] for m in moves)
    print(f"=== EDGE LATENCY ANALYSIS  {out_dir} ===")
    print(f"detections={len(dets)} book_moves={len(moves)}")

    detect_lags = [d["detect_lag_s"] for d in dets
                   if d.get("kind") == "tweet" and isinstance(d.get("detect_lag_s"), (int, float))
                   and 0 < d["detect_lag_s"] < 600]
    if detect_lags:
        detect_lags.sort()
        print("\n[1] OUR detection latency (t_detect - t_post), seconds:")
        print(f"    n={len(detect_lags)} min={detect_lags[0]:.1f} "
              f"p50={statistics.median(detect_lags):.1f} "
              f"p90={detect_lags[int(0.9 * len(detect_lags))]:.1f} max={detect_lags[-1]:.1f}")
    else:
        print("\n[1] no fresh tweet detections with a recoverable post time.")

    # For each tweet/count detection, find the nearest book move (signed: + = market AFTER us)
    triggers = [d for d in dets if d.get("kind") in ("tweet", "count")]
    rel = []
    for d in triggers:
        t = d.get("t_detect")
        if not t:
            continue
        j = bisect.bisect_left(move_ts, t)
        cand = []
        if j < len(move_ts):
            cand.append(move_ts[j] - t)
        if j > 0:
            cand.append(move_ts[j - 1] - t)
        cand = [c for c in cand if abs(c) <= window_s]
        if cand:
            rel.append(min(cand, key=abs))
    if rel:
        rel.sort()
        after = [r for r in rel if r > 0]
        before = [r for r in rel if r < 0]
        print(f"\n[2] nearest book move vs OUR detect (±{window_s:.0f}s), n={len(rel)}:")
        print(f"    market moved AFTER us (we were first): {len(after)}")
        print(f"    market moved BEFORE us (we were late):  {len(before)}")
        print(f"    median signed gap = {statistics.median(rel):+.1f}s  (positive => our edge window)")
        if after:
            print(f"    when we were first, median lead = {statistics.median(after):.1f}s")
    else:
        print("\n[2] no book moves within window of any detection — "
              "either quiet period or books didn't react.")
    print("\nVERDICT: edge exists iff [1] detection latency is reliably SHORTER than")
    print("the time the market takes to reprice (positive gaps in [2]).")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="3-clock edge latency capture")
    p.add_argument("--duration", type=float, default=1800.0, help="capture seconds")
    p.add_argument("--book-hz", type=float, default=4.0, help="book snapshot rate")
    p.add_argument("--slugs", nargs="*", default=None, help="override event slugs")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--analyze", default=None, help="analyze an existing out_dir instead of capturing")
    args = p.parse_args(argv)

    if args.analyze:
        analyze(Path(args.analyze))
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"data/edge_latency/{stamp}")
    capture(args.slugs or DEFAULT_SLUGS, args.duration, out_dir, args.book_hz)
    analyze(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
