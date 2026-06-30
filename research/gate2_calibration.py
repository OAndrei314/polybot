"""Gate 2 — calibration check on closed paper trades.

The question: when the model said "this trade has X% chance of making
money," did those trades actually make money X% of the time?

Method:
  1. For each competitor, read trades.jsonl. Group by (event, label, side).
  2. Within each group, FIFO-pair BUYs with subsequent SELLs.
  3. predicted = BUY's `prob_make_money` (entry-time forecast).
     realized = SELL's `pnl > 0` (1 if trade made money, else 0).
  4. Bucket predicted into 10 deciles; per bucket report mean predicted vs
     realized win rate.
  5. Verdict: calibrated if every bucket with n>=20 has |predicted - realized| <= 0.15.

Outputs:
  <out-dir>/calibration.csv          - bucket table
  <out-dir>/calibration.png          - calibration plot (predicted vs realized)
  stdout                             - verdict + summary

Usage:
  python -m research.gate2_calibration                        # v4 default
  python -m research.gate2_calibration --run-dir <path>
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
from research.run_select import latest_run

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


BUCKET_EDGES = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.000001]
MIN_BUCKET_N = 20
MAX_ABS_ERR = 0.15  # 15 pp


def collect_pairs(run_dir: Path) -> list[tuple[float, bool, str]]:
    """Return list of (predicted_prob_make_money_at_BUY, realized_won, reason)."""
    pairs: list[tuple[float, bool, str]] = []
    for fp in glob.glob(str(run_dir / "competitors" / "*" / "trades.jsonl")):
        # group by (event, label, side) within this competitor
        groups: dict[tuple, list[dict]] = defaultdict(list)
        with open(fp, "rb") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    t = json.loads(ln)
                except Exception:
                    continue
                if t.get("action") not in ("BUY", "SELL"):
                    continue
                key = (t.get("event_slug"), t.get("label"), t.get("side"))
                groups[key].append(t)
        for key, trades in groups.items():
            trades.sort(key=lambda x: x.get("ts") or "")
            buy_queue: deque[dict] = deque()
            for t in trades:
                if t["action"] == "BUY":
                    buy_queue.append(t)
                else:  # SELL
                    if not buy_queue:
                        continue  # unmatched SELL (shouldn't happen) - skip
                    b = buy_queue.popleft()
                    pred = b.get("prob_make_money")
                    pnl = t.get("pnl")
                    if pred is None or pnl is None:
                        continue
                    try:
                        pred = float(pred)
                        pnl = float(pnl)
                    except Exception:
                        continue
                    if not (0.0 <= pred <= 1.0):
                        continue
                    pairs.append((pred, pnl > 0.0, str(t.get("reason") or "?")))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--base-dir", default="data/reports/live_competitor_tests")
    ap.add_argument("--profile", default="v4", choices=["v4", "v5", "latest"])
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else latest_run(Path(args.base_dir), args.profile)
    out_dir = Path(args.out_dir) if args.out_dir else run_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs = collect_pairs(run_dir)
    if not pairs:
        print(f"no matched BUY/SELL pairs found in {run_dir}")
        return 1

    preds = np.array([p for p, _, _ in pairs])
    won = np.array([1 if w else 0 for _, w, _ in pairs])

    print(f"run: {run_dir.name}")
    print(f"matched BUY-SELL pairs: {len(pairs)}")
    print(f"overall predicted mean: {preds.mean():.4f}")
    print(f"overall realized win rate: {won.mean():.4f}")
    print(f"overall predicted vs realized error: {won.mean() - preds.mean():+.4f}")
    print()

    rows = []
    print(f"{'bucket':>15s} {'n':>6s} {'mean_pred':>10s} {'realized':>10s} {'error':>8s}  {'verdict':>10s}")
    print("-" * 70)
    worst_err = 0.0
    worst_label = ""
    for i in range(len(BUCKET_EDGES) - 1):
        lo, hi = BUCKET_EDGES[i], BUCKET_EDGES[i + 1]
        mask = (preds >= lo) & (preds < hi)
        n = int(mask.sum())
        if n == 0:
            row = {"bucket_lo": lo, "bucket_hi": min(hi, 1.0), "n": 0,
                   "mean_predicted": None, "realized_rate": None, "abs_error": None}
            rows.append(row)
            continue
        mp = float(preds[mask].mean())
        rw = float(won[mask].mean())
        err = abs(mp - rw)
        verdict = ""
        if n >= MIN_BUCKET_N:
            verdict = "OK" if err <= MAX_ABS_ERR else "FAIL"
            if err > worst_err:
                worst_err = err
                worst_label = f"[{lo:.1f},{hi:.1f})"
        else:
            verdict = "(small n)"
        print(f"  [{lo:.1f},{hi:.1f})    {n:>6d} {mp:>10.4f} {rw:>10.4f} {mp-rw:>+8.4f}  {verdict:>10s}")
        rows.append({"bucket_lo": lo, "bucket_hi": min(hi, 1.0), "n": n,
                     "mean_predicted": round(mp, 4), "realized_rate": round(rw, 4),
                     "abs_error": round(err, 4)})

    csv_path = out_dir / "calibration.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["bucket_lo", "bucket_hi", "n",
                                          "mean_predicted", "realized_rate", "abs_error"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {csv_path}")

    # Verdict
    print()
    if not worst_label:
        print("VERDICT: no bucket had n>=20; calibration test underpowered on this run.")
    elif worst_err <= MAX_ABS_ERR:
        print(f"VERDICT: CALIBRATED. Worst bucket error = {worst_err:.4f} ({worst_label}), "
              f"within the +/- {MAX_ABS_ERR:.2f} threshold.")
    else:
        gap_pp = worst_err * 100
        direction = ("model OVERCONFIDENT" if preds.mean() > won.mean()
                     else "model UNDERCONFIDENT")
        print(f"VERDICT: MISCALIBRATED. Worst bucket error = {gap_pp:.1f} pp "
              f"({worst_label}), exceeds the +/- {MAX_ABS_ERR*100:.0f} pp threshold.")
        print(f"  Direction overall: {direction} "
              f"(pred {preds.mean():.3f} vs realized {won.mean():.3f}).")

    # Plot
    fig, ax = plt.subplots(figsize=(7, 7))
    bucket_mids = []
    bucket_realized = []
    bucket_n = []
    for r in rows:
        if r["mean_predicted"] is not None and r["n"] >= 5:
            bucket_mids.append(r["mean_predicted"])
            bucket_realized.append(r["realized_rate"])
            bucket_n.append(r["n"])
    ax.plot([0, 1], [0, 1], color="#9ca3af", lw=1.5, ls="--", label="perfect calibration")
    if bucket_mids:
        sizes = [40 + 6 * np.sqrt(n) for n in bucket_n]
        ax.scatter(bucket_mids, bucket_realized, s=sizes,
                   color="#2563eb", alpha=0.8, edgecolor="black", zorder=3,
                   label="bucket means (size = sqrt n)")
        # +/- 15pp band
        x = np.linspace(0, 1, 100)
        ax.fill_between(x, x - MAX_ABS_ERR, x + MAX_ABS_ERR,
                        color="#86efac", alpha=0.18, label=f"+/- {MAX_ABS_ERR:.2f} band")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.set_xlabel("predicted prob_make_money (at entry)")
    ax.set_ylabel("realized win rate")
    ax.set_title(f"Gate 2 calibration  ({len(pairs)} matched pairs, "
                 f"{run_dir.name})", fontsize=10)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left", fontsize=9)
    png_path = out_dir / "calibration.png"
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)
    print(f"wrote {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
