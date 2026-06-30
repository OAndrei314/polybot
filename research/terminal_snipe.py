"""
Terminal-window snipe engine.

The only structurally-real edge on the Elon tweet markets is LATENCY: when a
tweet pushes the count across (or toward) a bracket boundary in the final hours
before close, the now-near-certain bracket is briefly mispriced until the order
book reprices (~6s, measured). This engine arms that edge on the fastest
detection we have (SSE push pool + graphql fallback) and:

  1. tracks the live period count per active market (fast path, not slow xtracker)
  2. runs the calibrated model each tick; in a market's terminal window it acts
     on large model-vs-market edges (paper) — buying the underpriced bracket
  3. SELF-MEASURES the edge: on every count change it records our detect time,
     then watches the affected book for the next reprice, logging our lead. If
     our lead is reliably positive, the edge is real; if not, it isn't.

Paper only — proves/quantifies the edge before any real capital. Run:
    python -m research.terminal_snipe --window-h 2 --edge 0.10 --duration 86400
Then inspect data/edge_latency/snipe/{signals,trades,latency}.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# SSE push stalls/idles out under X (curl 28 "operation too slow"), so it is NOT
# the durable fast path. The workhorse is parallel graphql polling across the
# account pool: sustainable detection cadence lane_s ~= 18s / N_accounts (the
# per-cookie ~50 req/15min budget). Sub-second detection (to beat the ~6s book
# reprice) therefore needs ~100 accounts, not SSE. Default SSE off.
os.environ.setdefault("POLYBOT_ENABLE_SSE_POOL", "0")

from market.prices import PolymarketPrices
from signals.stream import TweetStream
from signals.health import XTrackerHealth
from model.predictor import PredictionModel

XTRACKER = "https://xtracker.polymarket.com/api"
OUT = Path("data/edge_latency/snipe")


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _final_count(start_iso: str, end_iso: str) -> int | None:
    s = start_iso.replace("+00:00", "Z")
    e = end_iso.replace("+00:00", "Z")
    u = (f"{XTRACKER}/users/elonmusk/posts"
         f"?startDate={urllib.parse.quote(s)}&endDate={urllib.parse.quote(e)}")
    try:
        d = requests.get(u, timeout=10).json().get("data")
        return len(d) if isinstance(d, list) else None
    except Exception:
        return None


class SnipeEngine:
    def __init__(self, window_h: float, edge: float, tick_s: float,
                 min_price: float, max_price: float, max_cost: float,
                 duration_s: float) -> None:
        self.window_h = window_h
        self.edge_thr = edge
        self.tick_s = tick_s
        self.min_price = min_price
        self.max_price = max_price
        self.max_cost = max_cost
        self.duration_s = duration_s

        OUT.mkdir(parents=True, exist_ok=True)
        self.sig_f = (OUT / "signals.jsonl").open("a", buffering=1)
        self.trade_f = (OUT / "trades.jsonl").open("a", buffering=1)
        self.lat_f = (OUT / "latency.jsonl").open("a", buffering=1)

        self.feed = PolymarketPrices()
        self.stream = TweetStream()
        self.health = XTrackerHealth()
        self.model = PredictionModel()

        self.positions: dict[str, dict] = {}     # key slug|label -> position
        self.counts: dict[str, int] = {}          # slug -> last seen count
        self.count_change_at: dict[str, float] = {}  # slug -> t of last count change (pending reprice watch)
        self.last_ask: dict[tuple, float] = {}    # (slug,label) -> last ask
        self.windows: dict[str, tuple[str, str]] = {}  # slug -> (start,end)
        self.n_sig = 0
        self.n_trade = 0
        self.n_lat = 0

    # ── detection / measurement ────────────────────────────────────────────
    def _current_count(self, slug: str, tweets: dict, health: dict) -> int | None:
        elc = (tweets.get("event_live_counts") or {}).get(slug)
        if isinstance(elc, int):
            return elc
        ev = (health.get("events") or {}).get(slug) or {}
        if ev.get("ok") and isinstance(ev.get("count"), int):
            return ev["count"]
        xpc = (tweets.get("xtracker_period_counts") or {}).get(slug)
        return xpc if isinstance(xpc, int) else None

    def _watch_counts(self, prices, tweets, health, now):
        # detect count changes (the trigger) per slug
        seen = set()
        for ev_slug, ev in (health.get("events") or {}).items():
            if ev.get("start") and ev.get("end"):
                self.windows[ev_slug] = (ev["start"], ev["end"])
        for row in prices:
            slug = row["event_slug"]
            if slug in seen:
                continue
            seen.add(slug)
            c = self._current_count(slug, tweets, health)
            if c is None:
                continue
            prev = self.counts.get(slug)
            if prev is not None and c != prev:
                # COUNT CHANGE = the edge trigger. record detect time, open reprice watch.
                self.count_change_at[slug] = now
                rec = {"kind": "count_change", "ts": _iso(now), "slug": slug,
                       "prev": prev, "new": c, "source": tweets.get("count_source"),
                       "sse_alive": (tweets.get("sse") or {}).get("alive")}
                self.lat_f.write(json.dumps(rec) + "\n")
                print(f"[COUNT] {slug} {prev}->{c} src={tweets.get('count_source')} "
                      f"sse={tweets.get('sse',{}).get('alive')}")
            self.counts[slug] = c

    def _watch_reprice(self, prices, now):
        # if a market with a pending count-change sees a book move, record our LEAD
        for row in prices:
            slug, lab = row["event_slug"], row["label"]
            ask = row.get("yes_buy")
            if ask is None:
                continue
            key = (slug, lab)
            prev = self.last_ask.get(key)
            self.last_ask[key] = ask
            if prev is None:
                continue
            if abs(ask - prev) >= 0.01 and slug in self.count_change_at:
                lead = now - self.count_change_at.pop(slug)
                rec = {"kind": "reprice_after_count", "ts": _iso(now), "slug": slug,
                       "label": lab, "old_ask": prev, "new_ask": ask,
                       "our_lead_s": round(lead, 3)}
                self.lat_f.write(json.dumps(rec) + "\n")
                self.n_lat += 1
                print(f"[LEAD ] {slug} {lab} book moved {prev:.3f}->{ask:.3f} "
                      f"{lead:.2f}s after we saw the count change "
                      f"({'WE LED' if lead < 0 else 'book led' if lead > 2 else 'tie'})")

    # ── paper execution ────────────────────────────────────────────────────
    def _maybe_enter(self, pred, now):
        slug, lab = pred["event_slug"], pred["label"]
        key = f"{slug}|{lab}"
        if key in self.positions:
            return
        if pred["signal"] != "BUY_YES" or pred["edge"] < self.edge_thr:
            return
        ask = pred["market_p_yes"]  # mid proxy; entry uses ask below if present
        price = pred.get("yes_buy", ask)
        if not (self.min_price <= price <= self.max_price):
            return
        hours_left = pred["hours_left"]
        terminal = hours_left <= self.window_h
        size = min(self.max_cost / max(price, 0.01), self.max_cost)
        pos = {"slug": slug, "label": lab, "entry_price": price, "size": size,
               "opened_at": _iso(now), "hours_left_at_entry": hours_left,
               "terminal": terminal, "model_p": pred["model_p_yes"],
               "edge_at_entry": pred["edge"], "current_count": pred["current_count"]}
        self.positions[key] = pos
        self.trade_f.write(json.dumps({"kind": "OPEN", **pos}) + "\n")
        self.n_trade += 1
        tag = "TERMINAL-SNIPE" if terminal else "edge"
        print(f"[OPEN ] {tag} {key} @ {price:.3f} edge={pred['edge']:+.3f} "
              f"hrs_left={hours_left:.1f} cur={pred['current_count']}")

    def _settle_closed(self, now):
        for key, pos in list(self.positions.items()):
            slug = pos["slug"]
            win = self.windows.get(slug)
            if not win:
                continue
            end_ts = datetime.fromisoformat(win[1].replace("Z", "+00:00")).timestamp()
            if now < end_ts + 120:
                continue
            fc = _final_count(win[0], win[1])
            if fc is None:
                continue
            import re
            m = re.match(r"^(\d+)-(\d+)$", pos["label"])
            won = 1 if (m and int(m.group(1)) <= fc <= int(m.group(2))) else 0
            pnl = pos["size"] * ((1.0 - pos["entry_price"]) if won else (-pos["entry_price"]))
            rec = {"kind": "SETTLE", **pos, "final_count": fc, "won": won, "pnl": round(pnl, 2)}
            self.trade_f.write(json.dumps(rec) + "\n")
            print(f"[CLOSE] {key} final={fc} {'WIN' if won else 'LOSS'} pnl={pnl:+.2f}")
            del self.positions[key]

    # ── main loop ──────────────────────────────────────────────────────────
    def run(self):
        self.feed.start()
        self.stream.start()
        time.sleep(5)
        self.feed.refresh_stale_tokens(force_all=True)
        t_end = _now() + self.duration_s
        last_status = 0.0
        print(f"[snipe] running {self.duration_s:.0f}s  window_h={self.window_h} edge>={self.edge_thr}")
        while _now() < t_end:
            loop_t = _now()
            try:
                self.health.set_event_slugs(list(self.feed.event_slugs))
                prices = self.feed.snapshot()
                tweets = self.stream.poll()
                health = self.health.poll(tweets)

                self._watch_counts(prices, tweets, health, loop_t)
                self._watch_reprice(prices, loop_t)

                out = self.model.predict(prices, tweets, health, edge_threshold=self.edge_thr)
                for pred in out.get("predictions", []):
                    rec = {"ts": _iso(loop_t), **{k: pred[k] for k in
                           ("event_slug", "label", "current_count", "hours_left",
                            "model_p_yes", "market_p_yes", "edge", "signal")}}
                    if pred["signal"] != "HOLD" and pred["edge"] >= self.edge_thr:
                        self.sig_f.write(json.dumps(rec) + "\n")
                        self.n_sig += 1
                    self._maybe_enter(pred, loop_t)

                self._settle_closed(loop_t)

                if loop_t - last_status > 30:
                    last_status = loop_t
                    nearest = min((p["hours_left"] for p in out.get("predictions", [])),
                                  default=None)
                    print(f"[stat ] markets={len(self.feed.event_slugs)} "
                          f"sse={tweets.get('sse',{}).get('alive')}/{tweets.get('sse',{}).get('total')} "
                          f"signals={self.n_sig} trades={self.n_trade} leads={self.n_lat} "
                          f"open={len(self.positions)} nearest_close_h={nearest}")
            except Exception as e:
                print(f"[err ] {type(e).__name__}: {e}")
            slept = _now() - loop_t
            time.sleep(max(0.0, self.tick_s - slept))

        self.feed.stop()
        self.stream.stop()
        print(f"[snipe] done. signals={self.n_sig} trades={self.n_trade} leads_measured={self.n_lat}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="terminal-window snipe engine (paper + self-measuring)")
    p.add_argument("--window-h", type=float, default=2.0, help="terminal window for snipe tagging")
    p.add_argument("--edge", type=float, default=0.10, help="min model-vs-market edge to act")
    p.add_argument("--tick-s", type=float, default=0.5)
    p.add_argument("--min-price", type=float, default=0.03)
    p.add_argument("--max-price", type=float, default=0.92)
    p.add_argument("--max-cost", type=float, default=100.0)
    p.add_argument("--duration", type=float, default=86400.0)
    a = p.parse_args(argv)
    SnipeEngine(a.window_h, a.edge, a.tick_s, a.min_price, a.max_price,
                a.max_cost, a.duration).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
