"""Resolve orphaned positions and produce a corrected leaderboard.

THE BUG THIS FIXES
==================
When a Polymarket event ends, Polymarket removes it from the active events
list. The paper-trade engine's resolution-exit branch needs a live price row
to fire (`hours_left <= 0 AND current_count known`). Once the event vanishes
from the universe, those open positions become ORPHANED — still listed as
"open", never closed, valued at cost basis in the leaderboard.

Observed 2026-05-20: may-18-20 ended (count 73), Polymarket dropped it, 69 NO
positions on [40-64] were left orphaned. The leaderboard under-counted the
pool's true P&L by ~$3,000.

WHAT THIS SCRIPT DOES
=====================
1. Reads a run dir's trades + real_input_ticks.
2. Finds all open positions (BUYs without matching SELLs).
3. Determines which events are "ended" (not in the latest tick's event list).
4. For orphaned positions, resolves them at the deterministic outcome using
   the event's final observed count.
5. Writes `leaderboard_corrected.csv` with true per-competitor P&L.

Usage:
    python -m research.resolve_orphans                       # latest run
    python -m research.resolve_orphans --run-dir <path>
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import gzip
import json
import os
import re
from pathlib import Path

from research.run_select import latest_run as select_latest_run

try:
    import orjson as _fastjson
    _LOADS = _fastjson.loads
except ImportError:
    _LOADS = json.loads

LIVE_TESTS_DIR = Path("data/reports/live_competitor_tests")


def latest_run() -> Path | None:
    try:
        return select_latest_run(LIVE_TESTS_DIR, "v4")
    except FileNotFoundError:
        return None


def parse_bracket(label: str) -> tuple[int, int] | None:
    label = (label or "").strip()
    m = re.match(r"^(\d+)-(\d+)$", label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d+)\+$", label)
    if m:
        return int(m.group(1)), 10**9
    return None


def load_trades(run_dir: Path) -> list[dict]:
    trades: list[dict] = []
    for fp in (run_dir / "competitors").glob("*/trades.jsonl"):
        cid = fp.parent.name
        try:
            with fp.open("rb") as f:
                for raw in f:
                    if not raw.strip():
                        continue
                    try:
                        t = _LOADS(raw)
                    except Exception:
                        continue
                    t["_cid"] = cid
                    trades.append(t)
        except Exception:
            pass
    return trades


def event_feed_history(run_dir: Path) -> tuple[dict[str, int], set[str], dict[str, str]]:
    """Scan real_input_ticks once. Returns:
      final_count_per_event   — last xtracker period count seen per event
      events_with_live_rows   — events with bracket price rows in the LAST tick
      last_price_ts_per_event — last tick timestamp each event had price rows
                                (= the moment it dropped off / resolved)
    """
    gz = run_dir / "real_input_ticks.jsonl.gz"
    final_counts: dict[str, int] = {}
    last_price_ts: dict[str, str] = {}
    if not gz.exists():
        return final_counts, set(), last_price_ts
    last_price_events: set[str] = set()
    with gzip.open(gz, "rt") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = _LOADS(line)
            except Exception:
                continue
            ts = d.get("ts")
            xpc = (d.get("tweet_state") or {}).get("xtracker_period_counts") or {}
            for slug, c in xpc.items():
                try:
                    final_counts[slug] = int(c)
                except Exception:
                    pass
            rows = d.get("price_rows")
            if rows:
                evs = {r.get("event_slug") for r in rows if r.get("event_slug")}
                last_price_events = evs
                if ts:
                    for ev in evs:
                        last_price_ts[ev] = ts
    return final_counts, last_price_events, last_price_ts


def _fee_at(price: float, shares: float, fee_rate: float = 0.02) -> float:
    """Polymarket-style taker fee = rate * shares * p * (1-p). ~0 at p=0 or p=1."""
    return fee_rate * shares * price * (1.0 - price)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    run_dir = args.run_dir or latest_run()
    if run_dir is None:
        print("ERROR: no run dir found")
        return 2

    trades = load_trades(run_dir)
    final_counts, events_with_price_rows, last_price_ts = event_feed_history(run_dir)

    buys = [t for t in trades if t.get("action") == "BUY"]
    sells = [t for t in trades if t.get("action") == "SELL"]

    # An event is RESOLVED (its open positions orphaned) if EITHER:
    #   - the bot already fired ≥1 `resolution` exit for it (caught some, missed others), OR
    #   - it has NO bracket price rows in the latest tick (Polymarket delisted it)
    events_with_resolution_exits = {
        s.get("event_slug") for s in sells if s.get("reason") == "resolution"
    }
    all_traded_events = {b.get("event_slug") for b in buys}
    resolved_events = set()
    for ev in all_traded_events:
        if ev in events_with_resolution_exits or ev not in events_with_price_rows:
            resolved_events.add(ev)

    # Match SELLs to BUYs to find open positions
    sell_match: collections.Counter = collections.Counter()
    for s in sells:
        sell_match[(s["_cid"], s["event_slug"], s["label"], s["side"])] += 1

    open_positions = []
    for b in buys:
        k = (b["_cid"], b["event_slug"], b["label"], b["side"])
        if sell_match[k] > 0:
            sell_match[k] -= 1
        else:
            open_positions.append(b)

    # For each open position, classify: orphaned (event ended, not active) vs still-open
    correction_per_cid: dict[str, float] = collections.defaultdict(float)
    correction_per_cid_event: dict[tuple[str, str], float] = collections.defaultdict(float)
    orphan_detail: list[dict] = []
    still_open_count = 0

    for b in open_positions:
        ev = b["event_slug"]
        if ev not in resolved_events:
            # event still live — legitimately open, not orphaned
            still_open_count += 1
            continue
        # event has resolved → this open position is orphaned
        final = final_counts.get(ev)
        bounds = parse_bracket(b.get("label"))
        if final is None or bounds is None:
            continue
        low, high = bounds
        landed = low <= final <= high
        side = b.get("side")
        wins = (landed and side == "YES") or (not landed and side == "NO")
        outcome_price = 1.0 if wins else 0.0
        shares = float(b.get("shares") or 0.0)
        cost = float(b.get("cost") or 0.0)
        exit_fee = _fee_at(outcome_price, shares)
        resolved_pnl = shares * outcome_price - exit_fee - cost
        # The leaderboard currently values this position at cost basis → $0 P&L.
        # The correction is the full resolved P&L.
        correction_per_cid[b["_cid"]] += resolved_pnl
        correction_per_cid_event[(b["_cid"], ev)] += resolved_pnl
        orphan_detail.append({
            "cid": b["_cid"], "event": ev, "label": b["label"], "side": side,
            "final_count": final, "landed": landed, "wins": wins,
            "shares": round(shares, 2), "cost": round(cost, 2),
            "resolved_pnl": round(resolved_pnl, 2),
        })

    # Write orphan_corrections.json — used by plot_competitor_pnl to apply the
    # correction as a STEP at the event's actual resolution timestamp, so the
    # P&L timeseries plots reflect the true value too (not just the bar chart).
    events_json: dict[str, dict] = {}
    for (cid, ev), delta in correction_per_cid_event.items():
        ej = events_json.setdefault(ev, {
            "resolution_ts": last_price_ts.get(ev),
            "by_competitor": {},
        })
        ej["by_competitor"][cid] = round(ej["by_competitor"].get(cid, 0.0) + delta, 4)
    corrections_json = {
        "by_competitor": {c: round(v, 4) for c, v in correction_per_cid.items()},
        "events": events_json,
    }
    (run_dir / "orphan_corrections.json").write_text(json.dumps(corrections_json, indent=2))

    # Load current leaderboard
    lb_path = run_dir / "leaderboard_latest.csv"
    rows = []
    with lb_path.open() as f:
        for r in csv.DictReader(f):
            rows.append(r)

    # Apply correction
    corrected = []
    for r in rows:
        cid = r.get("competitor_id")
        orig = float(r.get("total_pnl") or 0)
        delta = correction_per_cid.get(cid, 0.0)
        corrected.append({
            "competitor_id": cid,
            "reported_total_pnl": round(orig, 2),
            "orphan_correction": round(delta, 2),
            "corrected_total_pnl": round(orig + delta, 2),
            "open_positions": r.get("open_positions"),
        })
    corrected.sort(key=lambda x: -x["corrected_total_pnl"])

    out = run_dir / "leaderboard_corrected.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["competitor_id", "reported_total_pnl",
                                          "orphan_correction", "corrected_total_pnl",
                                          "open_positions"])
        w.writeheader()
        for c in corrected:
            w.writerow(c)

    # Summary
    reported_sum = sum(c["reported_total_pnl"] for c in corrected)
    corrected_sum = sum(c["corrected_total_pnl"] for c in corrected)
    n_orphans = len(orphan_detail)
    orphan_wins = sum(1 for o in orphan_detail if o["wins"])

    if not args.quiet:
        print(f"run: {run_dir.name}")
        print(f"open positions total: {len(open_positions)}")
        print(f"  still-open (event live): {still_open_count}")
        print(f"  orphaned (event ended, not closed): {n_orphans}")
        print()
        # orphan breakdown by event
        by_ev = collections.Counter()
        by_ev_pnl: dict = collections.defaultdict(float)
        for o in orphan_detail:
            by_ev[o["event"]] += 1
            by_ev_pnl[o["event"]] += o["resolved_pnl"]
        print("orphans by event:")
        for ev, n in by_ev.most_common():
            print(f"  {ev:46s}  n={n:4d}  resolved_pnl=${by_ev_pnl[ev]:+,.2f}")
        print()
        print(f"orphan win rate: {orphan_wins}/{n_orphans}")
        print(f"total orphan correction: ${corrected_sum - reported_sum:+,.2f}")
        print()
        print(f"=== POOL TOTAL ===")
        print(f"  reported (leaderboard):  ${reported_sum:+,.2f}")
        print(f"  CORRECTED (true value):  ${corrected_sum:+,.2f}")
        print()
        print(f"wrote {out}")
        print()
        print("=== TOP 10 (corrected) ===")
        for c in corrected[:10]:
            print(f"  {c['competitor_id']:24s}  reported=${c['reported_total_pnl']:+8.2f}  "
                  f"+orphan=${c['orphan_correction']:+7.2f}  =  ${c['corrected_total_pnl']:+8.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
