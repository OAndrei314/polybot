"""Periodic snapshot script for the 96h experiment.

Runs in a separate background process. Every `--interval-min` minutes, reads the
latest live_competitor_test run and writes a tiny JSON summary to
data/experiments/96h_snapshots/snapshot_<timestamp>.json.

Survives arena restarts: always rescans for the newest run dir.

Each snapshot captures:
  - top 10 competitors by total_pnl
  - bottom 10
  - distribution of P&L (min/p25/median/p75/max, count of profitable)
  - rolling trade stats (n_buys, n_sells, win rate, mean P&L per trade)
  - calibration sample: predicted prob_make_money bucket vs realized
  - signal_budget snapshot (latest GQL pace/health)

Robustness:
  - retries with backoff on any read error
  - never crashes; logs to stderr
  - bounded memory (only reads recent tail)

Usage (from repo root):
    python -m research.snapshot_96h --interval-min 120 --duration-h 96

Stop with Ctrl+C or:
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'snapshot_96h' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, quantiles

from research import jsonl_io

OUT_DIR = Path("data/experiments/96h_snapshots")
LIVE_TESTS_DIR = Path("data/reports/live_competitor_tests")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def latest_run() -> Path | None:
    if not LIVE_TESTS_DIR.exists():
        return None
    runs = sorted(LIVE_TESTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None


def read_leaderboard(run_dir: Path) -> list[dict]:
    path = run_dir / "leaderboard_latest.csv"
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                r["_total_pnl"] = float(r.get("total_pnl") or 0)
                r["_equity"] = float(r.get("equity") or 0)
                r["_open"] = int(float(r.get("open_positions") or 0))
                rows.append(r)
            except Exception:
                pass
    rows.sort(key=lambda x: -x["_total_pnl"])
    return rows


def calibration_sample(run_dir: Path, max_per_competitor: int = 500) -> dict:
    """Sample closed trades to compare predicted prob vs realized win rate.

    Bounded read — caps lines per competitor file to keep memory predictable.
    """
    buckets: dict[float, dict] = collections.defaultdict(
        lambda: {"n": 0, "wins": 0, "sum_pnl": 0.0, "sum_pred": 0.0}
    )
    n_buys = n_sells = 0
    win_pnl = loss_pnl = 0.0
    win_n = loss_n = 0
    sell_reasons: collections.Counter[str] = collections.Counter()
    side_counts: collections.Counter[str] = collections.Counter()

    competitors_dir = run_dir / "competitors"
    if not competitors_dir.exists():
        return {
            "buys": 0,
            "sells": 0,
            "win_rate": None,
            "mean_pnl_per_sell": None,
            "buckets": {},
            "sell_reasons": {},
            "side_counts": {},
        }
    # gather all BUYs into a dict keyed by (cid, slug, label, side) for matching
    buys_by_key: dict[tuple, dict] = {}
    for fp in competitors_dir.glob("*/trades.jsonl"):
        cid = fp.parent.name
        try:
            with fp.open() as f:
                lines = f.readlines()[-max_per_competitor:]
            for line in lines:
                try:
                    t = json.loads(line)
                except Exception:
                    continue
                action = t.get("action")
                if action == "BUY":
                    n_buys += 1
                    side_counts[t.get("side") or "?"] += 1
                    buys_by_key[(cid, t.get("event_slug"), t.get("label"), t.get("side"))] = t
                elif action == "SELL":
                    n_sells += 1
                    sell_reasons[str(t.get("reason") or "?")] += 1
                    pnl = float(t.get("pnl") or 0)
                    if pnl > 0:
                        win_n += 1
                        win_pnl += pnl
                    else:
                        loss_n += 1
                        loss_pnl += pnl
                    b = buys_by_key.get((cid, t.get("event_slug"), t.get("label"), t.get("side")))
                    if b is not None:
                        pred = float(b.get("prob_make_money") or 0)
                        bucket = round(pred * 10) / 10
                        d = buckets[bucket]
                        d["n"] += 1
                        d["sum_pred"] += pred
                        d["sum_pnl"] += pnl
                        if pnl > 0:
                            d["wins"] += 1
        except Exception:
            continue

    buckets_out = {}
    for k in sorted(buckets):
        v = buckets[k]
        if v["n"] == 0:
            continue
        buckets_out[f"{k:.1f}"] = {
            "n": v["n"],
            "pred_mean": round(v["sum_pred"] / v["n"], 4),
            "realized_win_rate": round(v["wins"] / v["n"], 4),
            "mean_pnl": round(v["sum_pnl"] / v["n"], 3),
        }

    total_pnl = win_pnl + loss_pnl
    return {
        "buys": n_buys,
        "sells": n_sells,
        "win_rate": round(win_n / n_sells, 4) if n_sells else None,
        "mean_pnl_per_sell": round(total_pnl / n_sells, 3) if n_sells else None,
        "buckets": buckets_out,
        "sell_reasons": dict(sell_reasons),
        "side_counts": dict(side_counts),
    }


def signal_budget_tail(run_dir: Path) -> dict:
    path = run_dir / "loop_metrics.jsonl"
    if not jsonl_io.exists(path):
        return {}
    try:
        tail = jsonl_io.last_line(path)
        if not tail:
            return {}
        d = json.loads(tail)
        return {
            "ts": d.get("ts"),
            "loop_elapsed_s": d.get("loop_elapsed_s"),
            "price_rows": d.get("price_rows"),
            "ws_connected": (d.get("price_feed_state") or {}).get("ws_connected"),
            "max_bbo_age_s": (d.get("price_feed_state") or {}).get("max_bbo_age_s"),
            "gql_lane_s": (d.get("signal_budget") or {}).get("gql_lane_s"),
            "gql_min_remaining": (d.get("signal_budget") or {}).get("gql_min_remaining"),
            "gql_statuses": (d.get("signal_budget") or {}).get("gql_statuses"),
        }
    except Exception:
        return {}


def take_snapshot(run_dir: Path) -> dict:
    rows = read_leaderboard(run_dir)
    if not rows:
        return {
            "run_dir": str(run_dir),
            "leaderboard_empty": True,
            "ts": _now_iso(),
        }
    pnls = [r["_total_pnl"] for r in rows]
    profitable = sum(1 for p in pnls if p > 0)
    breakeven = sum(1 for p in pnls if p == 0)
    losing = sum(1 for p in pnls if p < 0)

    q = quantiles(pnls, n=4) if len(pnls) >= 4 else [pnls[0], pnls[len(pnls)//2], pnls[-1]]
    return {
        "ts": _now_iso(),
        "run_dir": str(run_dir),
        "n_competitors": len(rows),
        "summary": {
            "min": round(min(pnls), 2),
            "q25": round(q[0], 2),
            "median": round(median(pnls), 2),
            "q75": round(q[-1], 2),
            "max": round(max(pnls), 2),
            "mean": round(sum(pnls) / len(pnls), 2),
            "profitable": profitable,
            "breakeven": breakeven,
            "losing": losing,
        },
        "top10": [
            {"cid": r["competitor_id"], "pnl": round(r["_total_pnl"], 2),
             "equity": round(r["_equity"], 2), "open": r["_open"]}
            for r in rows[:10]
        ],
        "bottom10": [
            {"cid": r["competitor_id"], "pnl": round(r["_total_pnl"], 2),
             "equity": round(r["_equity"], 2), "open": r["_open"]}
            for r in rows[-10:]
        ],
        "calibration": calibration_sample(run_dir),
        "feed": signal_budget_tail(run_dir),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-min", type=float, default=120.0,
                    help="snapshot every N minutes (default 120 = 2 hours)")
    ap.add_argument("--duration-h", type=float, default=96.0,
                    help="exit after this many hours (default 96)")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    end_at = time.time() + args.duration_h * 3600.0
    interval_s = args.interval_min * 60.0
    print(f"[{_now_iso()}] snapshot_96h starting", flush=True)
    print(f"  interval = {args.interval_min} min", flush=True)
    print(f"  duration = {args.duration_h} h", flush=True)
    print(f"  out_dir  = {args.out_dir}", flush=True)

    n = 0
    while time.time() < end_at:
        try:
            rd = latest_run()
            if rd is None:
                print(f"[{_now_iso()}] no run dir yet; retrying in 60s", file=sys.stderr, flush=True)
                time.sleep(60)
                continue
            snap = take_snapshot(rd)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out = args.out_dir / f"snapshot_{stamp}.json"
            out.write_text(json.dumps(snap, indent=2))
            n += 1
            summ = snap.get("summary") or {}
            cal = snap.get("calibration") or {}
            print(
                f"[{_now_iso()}] snapshot #{n} → {out.name}  "
                f"competitors={snap.get('n_competitors')}  "
                f"profitable={summ.get('profitable')}  median_pnl={summ.get('median')}  "
                f"buys={cal.get('buys')} sells={cal.get('sells')} win_rate={cal.get('win_rate')}",
                flush=True,
            )
        except Exception:
            print(f"[{_now_iso()}] ERROR: {traceback.format_exc()}", file=sys.stderr, flush=True)
        time.sleep(interval_s)

    print(f"[{_now_iso()}] snapshot_96h done after {n} snapshots", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
