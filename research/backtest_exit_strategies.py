"""Replay-style backtest of exit strategies on a recorded arena run.

For each BUY recorded in the run's competitor trades.jsonl files, we replay
the position against the recorded `real_input_ticks.jsonl.gz` price history.
At each tick after entry, we apply a candidate exit policy and decide whether
to close. Final price = either policy exit OR last observed price (= proxy
for hold-to-resolution outcome).

This lets us cleanly A/B different exit strategies on the EXACT data the live
arena saw, without needing to re-run the live arena.

Strategies tested (per run):
  1. Baseline (as recorded — what actually happened)
  2. Pure hold-to-resolution (last observed price)
  3. TP=0.40 SL=0.45, edge_decay OFF
  4. TP=0.50 SL=0.50, edge_decay OFF
  5. TP=0.30 SL=0.30, edge_decay OFF
  6. TP=DISABLED SL=0.50, edge_decay OFF
  7. TP=0.20 SL=0.40, edge_decay OFF

Usage from repo root:
    python -m research.backtest_exit_strategies --run-dir <path>
    python -m research.backtest_exit_strategies   # auto-pick latest
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class ExitPolicy:
    name: str
    take_profit_pct: float | None      # None = no TP
    stop_loss_pct: float | None        # None = no SL
    stop_loss_dwell_s: float = 300.0   # dwell to confirm breach


def load_buys(run_dir: Path) -> list[dict]:
    buys = []
    for fp in (run_dir / "competitors").glob("*/trades.jsonl"):
        cid = fp.parent.name
        try:
            with fp.open() as f:
                for line in f:
                    try:
                        t = json.loads(line)
                    except Exception:
                        continue
                    if t.get("action") == "BUY":
                        t["_cid"] = cid
                        buys.append(t)
        except Exception:
            pass
    return buys


def load_price_history(run_dir: Path) -> dict[tuple[str, str], list[tuple[float, float, float]]]:
    """Returns {(event_slug, label): [(ts_unix, yes_mid, no_mid), ...]} sorted by ts."""
    gz = run_dir / "real_input_ticks.jsonl.gz"
    history: dict[tuple[str, str], list[tuple[float, float, float]]] = collections.defaultdict(list)
    if not gz.exists():
        return history

    with gzip.open(gz, "rt") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts_str = d.get("ts")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            for row in d.get("price_rows") or []:
                slug = row.get("event_slug")
                label = row.get("label")
                ym = row.get("yes_mid")
                nm = row.get("no_mid")
                if not slug or not label or ym is None or nm is None:
                    continue
                history[(slug, label)].append((ts, float(ym), float(nm)))
    # sort each list
    for k in history:
        history[k].sort()
    return history


def _fee_per_share(price: float, fee_rate: float = 0.02) -> float:
    """Polymarket-style: fee = rate * price * (1-price), per share."""
    return fee_rate * price * (1.0 - price)


def simulate_position(
    buy: dict,
    price_history: list[tuple[float, float, float]],
    policy: ExitPolicy,
    fee_rate: float = 0.02,
    slippage: float = 0.005,
) -> dict:
    """Simulate one position from BUY to exit under the given policy.
    Returns dict with exit_reason, exit_price, exit_ts, pnl, hold_seconds.
    """
    entry_price = float(buy["price"])
    entry_ts = datetime.fromisoformat(buy["ts"].replace("Z", "+00:00")).timestamp()
    shares = float(buy["shares"])
    side = buy["side"]  # "YES" or "NO"
    entry_fee_per_share = _fee_per_share(entry_price, fee_rate)
    cost_per_share = entry_price + entry_fee_per_share + slippage

    # Find price ticks after entry
    ticks_after = [(ts, ym, nm) for ts, ym, nm in price_history if ts >= entry_ts]
    if not ticks_after:
        # No future data; assume last known price
        return {
            "exit_reason": "no_data",
            "exit_price": entry_price,
            "exit_ts": entry_ts,
            "pnl": 0.0,
            "hold_seconds": 0.0,
        }

    # Determine which side we're holding
    def get_mark(ym: float, nm: float) -> float:
        return ym if side == "YES" else nm

    breach_since: float | None = None
    for ts, ym, nm in ticks_after:
        mark = get_mark(ym, nm)
        # gross pnl pct
        pnl_pct = (mark - cost_per_share) / cost_per_share if cost_per_share > 0 else 0.0

        # Take profit
        if policy.take_profit_pct is not None and pnl_pct >= policy.take_profit_pct:
            return _finalize_exit(ts, mark, side, shares, cost_per_share, fee_rate, slippage, "take_profit", entry_ts)

        # Stop loss (with dwell time)
        if policy.stop_loss_pct is not None and pnl_pct <= -policy.stop_loss_pct:
            if breach_since is None:
                breach_since = ts
            elif (ts - breach_since) >= policy.stop_loss_dwell_s:
                return _finalize_exit(ts, mark, side, shares, cost_per_share, fee_rate, slippage, "stop_loss", entry_ts)
        else:
            breach_since = None

    # Held to end of data — use LAST tick as exit. This proxies "held to resolution".
    last_ts, last_ym, last_nm = ticks_after[-1]
    mark = get_mark(last_ym, last_nm)
    return _finalize_exit(last_ts, mark, side, shares, cost_per_share, fee_rate, slippage, "resolution", entry_ts)


def _finalize_exit(ts, mark, side, shares, cost_per_share, fee_rate, slippage, reason, entry_ts):
    exit_fee_per_share = _fee_per_share(mark, fee_rate)
    proceeds_per_share = mark - exit_fee_per_share - slippage
    pnl = (proceeds_per_share - cost_per_share) * shares
    return {
        "exit_reason": reason,
        "exit_price": mark,
        "exit_ts": ts,
        "pnl": pnl,
        "hold_seconds": ts - entry_ts,
    }


def run_backtest(run_dir: Path, policies: list[ExitPolicy]) -> dict:
    print(f"Loading BUYs from {run_dir}...", flush=True)
    buys = load_buys(run_dir)
    print(f"  {len(buys)} BUY events", flush=True)
    print(f"Loading price history...", flush=True)
    price_history = load_price_history(run_dir)
    print(f"  {len(price_history)} (event,label) price series", flush=True)

    results: dict[str, dict] = {}
    for policy in policies:
        wins = 0
        pnls = []
        by_reason: collections.Counter[str] = collections.Counter()
        missing = 0
        for b in buys:
            key = (b.get("event_slug"), b.get("label"))
            ph = price_history.get(key)
            if not ph:
                missing += 1
                continue
            outcome = simulate_position(b, ph, policy)
            pnls.append(outcome["pnl"])
            by_reason[outcome["exit_reason"]] += 1
            if outcome["pnl"] > 0:
                wins += 1
        n = len(pnls)
        results[policy.name] = {
            "n": n,
            "missing_data": missing,
            "win_rate": wins / n if n else 0,
            "mean_pnl": sum(pnls) / n if n else 0,
            "total_pnl": sum(pnls),
            "by_reason": dict(by_reason),
        }
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, default=None)
    args = ap.parse_args()

    if args.run_dir is None:
        base = Path("data/reports/live_competitor_tests")
        runs = sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        # skip in-progress (look for one with completed real_input_ticks)
        for r in runs:
            if (r / "real_input_ticks.jsonl.gz").exists():
                args.run_dir = r
                break
        if args.run_dir is None:
            print("ERROR: no run dir with real_input_ticks.jsonl.gz found")
            return 2

    policies = [
        ExitPolicy("recorded_baseline_TP10_SL25_EDGE", 0.10, 0.25),  # roughly what v2 did
        ExitPolicy("hold_to_resolution", None, None),                # pure hold
        ExitPolicy("TP30_SL30_no_edge",  0.30, 0.30),
        ExitPolicy("TP40_SL45_no_edge",  0.40, 0.45),
        ExitPolicy("TP50_SL50_no_edge",  0.50, 0.50),
        ExitPolicy("noTP_SL50_no_edge",  None,  0.50),
        ExitPolicy("TP20_SL40_no_edge",  0.20, 0.40),
        ExitPolicy("TP80_SL30_no_edge",  0.80, 0.30),  # very tight stop, big TP
    ]

    results = run_backtest(args.run_dir, policies)

    print()
    print("=== BACKTEST RESULTS ===")
    print(f"Run: {args.run_dir.name}")
    print()
    print(f"{'policy':40s} {'n':>5s} {'win%':>6s} {'mean_pnl':>10s} {'total_pnl':>12s}")
    sorted_p = sorted(results.items(), key=lambda kv: -kv[1]["total_pnl"])
    for name, r in sorted_p:
        print(f"{name:40s} {r['n']:>5d} {r['win_rate']*100:>5.1f}% {r['mean_pnl']:>+10.2f} {r['total_pnl']:>+12.2f}")
    print()
    print(f"{'policy':40s}  exit_reason breakdown")
    for name, r in sorted_p:
        print(f"  {name:38s}  {r['by_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
