"""
Forward data collector to confirm-or-kill the favorite-overconfidence edge.

The 2026-06-08 study found a DIRECTIONAL hint that the market overprices its
favorite (modal) bracket 6-24h before close — fading it returned +10-14% per
market — but on only n=15 resolved markets the t-stat was ~1 (not significant).
This tool accumulates the sample needed to settle it.

Two modes:
  snapshot  — for every currently-active Elon tweet market, log all bracket
              prices + hours-to-close. Run on a schedule (e.g. hourly).
  resolve   — for windows that have closed, fetch the authoritative xtracker
              final count, mark the winning bracket, and append a clean
              per-market observation (favorite price at the nearest horizon,
              whether it won) to outcomes.jsonl.

After ~50-100 markets, run with --analyze to get the t-stat on fade-the-favorite
at each horizon. If |t|>2 and P&L>spread, it's a real, speed-free edge.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from math import sqrt

import requests

from market.prices import PolymarketPrices

XTRACKER = "https://xtracker.polymarket.com/api"
OUT = Path("data/edge_latency/favorite_bias")
HORIZONS_H = (24, 12, 6, 3, 1)


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse(lab: str):
    m = re.match(r"^(\d+)-(\d+)$", lab)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d+)\+$", lab)
    if m:
        return int(m.group(1)), 99999
    return None


def _active_elon_slugs(feed: PolymarketPrices) -> dict[str, tuple[str, str]]:
    """Discover active Elon tweet markets and their counting windows from xtracker."""
    out: dict[str, tuple[str, str]] = {}
    try:
        r = requests.get(f"{XTRACKER}/users/elonmusk/trackings?activeOnly=true", timeout=15)
        for tr in (r.json().get("data") or []):
            link = str(tr.get("marketLink") or "")
            slug = link.rstrip("/").rsplit("/", 1)[-1]
            s, e = tr.get("startDate"), tr.get("endDate")
            if slug and "tweets" in slug and s and e:
                out[slug] = (s, e)
    except Exception:
        pass
    return out


def cmd_snapshot() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    snap_f = (OUT / "snapshots.jsonl").open("a", buffering=1)
    feed = PolymarketPrices()  # auto-discovers active Elon markets
    feed.start()
    time.sleep(5)
    feed.refresh_stale_tokens(force_all=True)
    windows = _active_elon_slugs(feed)
    rows = feed.snapshot()
    now = _now()
    by_slug: dict[str, list] = {}
    for r in rows:
        by_slug.setdefault(r["event_slug"], []).append(r)
    n = 0
    for slug, brs in by_slug.items():
        win = windows.get(slug)
        rec = {
            "ts": _iso(now),
            "slug": slug,
            "window": win,
            "brackets": [
                {"label": b["label"], "yes_buy": b["yes_buy"], "yes_sell": b["yes_sell"]}
                for b in brs if b.get("yes_buy") is not None
            ],
        }
        snap_f.write(json.dumps(rec) + "\n")
        n += 1
    snap_f.close()
    feed.stop()
    print(f"[snapshot] logged {n} active markets at {_iso(now)}")
    return 0


def _xtracker_count(start: str, end: str) -> int | None:
    s = start.replace("+00:00", "Z")
    e = end.replace("+00:00", "Z")
    u = (f"{XTRACKER}/users/elonmusk/posts"
         f"?startDate={urllib.parse.quote(s)}&endDate={urllib.parse.quote(e)}")
    try:
        d = requests.get(u, timeout=15).json().get("data")
        return len(d) if isinstance(d, list) else None
    except Exception:
        return None


def cmd_resolve() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    snaps = []
    sp = OUT / "snapshots.jsonl"
    if sp.exists():
        for line in sp.read_text().splitlines():
            if line.strip():
                snaps.append(json.loads(line))
    done = set()
    op = OUT / "outcomes.jsonl"
    if op.exists():
        for line in op.read_text().splitlines():
            if line.strip():
                done.add(json.loads(line)["key"])

    # group snapshots by slug
    by_slug: dict[str, list] = {}
    for s in snaps:
        if s.get("window"):
            by_slug.setdefault(s["slug"], []).append(s)

    out_f = op.open("a", buffering=1)
    now = _now()
    added = 0
    for slug, ss in by_slug.items():
        win = ss[0]["window"]
        end_ts = datetime.fromisoformat(win[1].replace("Z", "+00:00")).timestamp()
        if now < end_ts + 600:  # not closed yet (+10min buffer)
            continue
        final = _xtracker_count(win[0], win[1])
        if final is None:
            continue
        # for each horizon, find the snapshot closest to it and record the favorite
        for H in HORIZONS_H:
            target = end_ts - H * 3600
            cand = [s for s in ss
                    if abs(datetime.fromisoformat(s["ts"]).timestamp() - target) <= 5400]
            if not cand:
                continue
            best = min(cand, key=lambda s: abs(datetime.fromisoformat(s["ts"]).timestamp() - target))
            brs = [b for b in best["brackets"]
                   if b["yes_buy"] is not None and b["yes_sell"] is not None]
            if not brs:
                continue
            fav = max(brs, key=lambda b: (b["yes_buy"] + b["yes_sell"]) / 2.0)
            mid = (fav["yes_buy"] + fav["yes_sell"]) / 2.0
            if not (0.05 < mid < 0.95):
                continue
            b = _parse(fav["label"])
            won = 1 if (b and b[0] <= final <= b[1]) else 0
            key = f"{slug}|{H}"
            if key in done:
                continue
            rec = {
                "key": key, "slug": slug, "horizon_h": H, "final": final,
                "favorite": fav["label"], "fav_mid": round(mid, 4),
                "fav_yes_buy": fav["yes_buy"], "fav_yes_sell": fav["yes_sell"], "won": won,
            }
            out_f.write(json.dumps(rec) + "\n")
            added += 1
    out_f.close()
    print(f"[resolve] added {added} clean per-market observations -> {op}")
    return 0


def cmd_analyze() -> int:
    op = OUT / "outcomes.jsonl"
    if not op.exists():
        print("no outcomes yet — run snapshot (scheduled) then resolve")
        return 1
    obs = [json.loads(l) for l in op.read_text().splitlines() if l.strip()]
    by_h: dict[int, list] = {}
    for o in obs:
        by_h.setdefault(o["horizon_h"], []).append(o)
    print(f"FADE-THE-FAVORITE confirmation  (total obs={len(obs)})")
    print("real, tradeable edge iff |t|>2 AND fade P&L exceeds the NO-side spread\n")
    for H in sorted(by_h, reverse=True):
        rows = by_h[H]
        # fade = buy NO at ask=(1-yes_sell); pays 1 if favorite loses. net = (1-won) - (1-yes_sell)
        net = [(1 - o["won"]) - (1 - o["fav_yes_sell"]) for o in rows]
        if len(net) < 2:
            continue
        m = mean(net); sd = pstdev(net); se = sd / sqrt(len(net)) if sd else 0
        t = m / se if se else 0
        favwon = sum(o["won"] for o in rows)
        print(f"  T-{H}h: n={len(rows):3} fade_net_pnl={m:+.3f} t={t:+.2f} "
              f"favorite_won={favwon}/{len(rows)} ({favwon/len(rows)*100:.0f}%)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="favorite-overconfidence edge collector")
    p.add_argument("mode", choices=["snapshot", "resolve", "analyze"])
    args = p.parse_args(argv)
    if args.mode == "snapshot":
        return cmd_snapshot()
    if args.mode == "resolve":
        return cmd_resolve()
    return cmd_analyze()


if __name__ == "__main__":
    raise SystemExit(main())
