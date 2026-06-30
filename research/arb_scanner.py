"""Cross-bracket arbitrage scanner for Polymarket Elon-tweet events.

THE QUESTION
------------
For each event (e.g., "Elon's tweets May 27-29"), the brackets are mutually
exclusive and exhaustive: exactly ONE resolves YES, all others resolve NO.
That gives two no-arbitrage identities:

  sum(yes_buy across brackets)  should be  approximately  $1.00
  sum(no_buy  across brackets)  should be  approximately  $(N-1)

If sum(yes_buy) < $1.00 by more than fees+slippage, you could buy YES on
every bracket and capture a guaranteed +$1 payout. Same for NO.

This scanner walks recorded tick archives, flags moments where the sums
diverge from no-arb, and quantifies how often and how much.

INTERPRETATION
--------------
  - "yes_gap" = 1.00 - sum(yes_buy)
                Positive means YES sides are collectively cheap.
                Profit per arb = yes_gap minus N*per-side fees+spread.
  - "no_gap"  = (N-1) - sum(no_buy)
                Positive means NO sides are collectively cheap.
                Profit per arb = no_gap minus N*per-side fees+spread.

A typical Polymarket taker fee is ~2% per side, plus half-spread (~1-3 cents
on $0.20-$0.50 quotes). For an N=5 event the total round-trip cost is
roughly 5*(0.04) = $0.20 of frictional drag. So a gap above ~$0.20 is the
threshold for a clearly profitable arb on a 5-bracket event.

Usage:
  python -m research.arb_scanner                              # scan v4 OLD baseline
  python -m research.arb_scanner --archive <path>             # one archive
  python -m research.arb_scanner --threshold 0.05             # 5 cent gap minimum
  python -m research.arb_scanner --all                        # scan all run archives

Outputs:
  <out-dir>/arb_opportunities.csv       all flagged moments
  <out-dir>/arb_summary.csv             aggregate stats per event
  <out-dir>/arb_gap_distribution.png    distribution of gap sizes
  <out-dir>/arb_gap_timeline.png        gap over time per event
  stdout                                verdict
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

DEFAULT_THRESHOLD = 0.01  # 1-cent gap minimum to flag (very permissive baseline)
# Approximate per-side execution cost (fee + half spread + slippage).
# Used to compute "fee-adjusted" net profit for the verdict.
PER_SIDE_COST = 0.04


def iter_ticks(archive_path: Path):
    """Yield each tick row from a real_input_ticks.jsonl.gz archive."""
    with gzip.open(archive_path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def scan_archive(archive_path: Path, threshold: float) -> list[dict]:
    opportunities: list[dict] = []
    for row in iter_ticks(archive_path):
        ts = row.get("ts")
        by_event: dict[str, list[dict]] = defaultdict(list)
        for pr in row.get("price_rows", []) or []:
            slug = pr.get("event_slug")
            if not slug or "elon-musk-of-tweets" not in slug:
                continue
            by_event[slug].append(pr)

        for slug, brackets in by_event.items():
            if len(brackets) < 2:
                continue
            yes_buys, no_buys = [], []
            incomplete = False
            for b in brackets:
                yb, nb = b.get("yes_buy"), b.get("no_buy")
                if yb is None or nb is None:
                    incomplete = True
                    break
                yes_buys.append(float(yb))
                no_buys.append(float(nb))
            if incomplete:
                continue
            n = len(brackets)
            sum_yes = sum(yes_buys)
            sum_no = sum(no_buys)
            yes_gap = 1.0 - sum_yes
            no_gap = (n - 1) - sum_no

            # Approximate net-of-cost profitability (assumes you take all N sides)
            yes_net = yes_gap - n * PER_SIDE_COST
            no_net = no_gap - n * PER_SIDE_COST

            if yes_gap >= threshold:
                opportunities.append({
                    "ts": ts, "event_slug": slug, "n_brackets": n,
                    "side": "YES", "sum": round(sum_yes, 4),
                    "gross_gap": round(yes_gap, 4),
                    "net_profit_after_fees": round(yes_net, 4),
                    "labels": ",".join(str(b.get("label","")) for b in brackets),
                    "prices": ",".join(f"{p:.3f}" for p in yes_buys),
                })
            if no_gap >= threshold:
                opportunities.append({
                    "ts": ts, "event_slug": slug, "n_brackets": n,
                    "side": "NO", "sum": round(sum_no, 4),
                    "gross_gap": round(no_gap, 4),
                    "net_profit_after_fees": round(no_net, 4),
                    "labels": ",".join(str(b.get("label","")) for b in brackets),
                    "prices": ",".join(f"{p:.3f}" for p in no_buys),
                })
    return opportunities


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=None,
                    help="single archive path (real_input_ticks.jsonl.gz)")
    ap.add_argument("--all", action="store_true",
                    help="scan every available archive in data/reports/live_competitor_tests/")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="minimum gross gap (USD) to flag. Default 0.01.")
    ap.add_argument("--out-dir", default="notebooks/png/arb")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    archives: list[Path] = []
    if args.archive:
        archives = [Path(args.archive)]
    elif args.all:
        archives = sorted(Path(".").glob(
            "data/reports/live_competitor_tests/*/real_input_ticks.jsonl.gz"))
    else:
        # default: v4 OLD baseline (7-day completed run = most data)
        default = Path("data/reports/live_competitor_tests/"
                       "20260517_194956_real_48h/real_input_ticks.jsonl.gz")
        if default.exists():
            archives = [default]

    if not archives:
        print("no archives found")
        return 1

    print(f"scanning {len(archives)} archive(s) with threshold ${args.threshold:.2f} gap")
    print(f"per-side cost assumption: ${PER_SIDE_COST:.3f} "
          f"(round-trip on N sides = ${PER_SIDE_COST*5:.3f} for N=5)")
    print()

    all_ops: list[dict] = []
    for arch in archives:
        run_name = arch.parent.name
        ops = scan_archive(arch, args.threshold)
        for o in ops:
            o["source_run"] = run_name
        all_ops.extend(ops)
        print(f"  {run_name}: {len(ops)} flagged moments")

    if not all_ops:
        print()
        print("=== ZERO opportunities flagged above threshold ===")
        print("Either the threshold is too high, or Polymarket bracket prices are")
        print("internally consistent at this granularity. Try a lower threshold")
        print("if you want to see near-arb moments.")
        return 0

    # Write CSV
    csv_path = out_dir / "arb_opportunities.csv"
    fields = ["ts", "source_run", "event_slug", "n_brackets", "side", "sum",
              "gross_gap", "net_profit_after_fees", "labels", "prices"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_ops)
    print(f"wrote {csv_path}")

    # Summary stats
    print()
    print(f"=== AGGREGATE ===")
    print(f"  total flagged opportunities: {len(all_ops)}")
    by_side = defaultdict(int)
    for o in all_ops:
        by_side[o["side"]] += 1
    for s, n in by_side.items():
        print(f"    {s}-side: {n}")
    gross_gaps = [o["gross_gap"] for o in all_ops]
    net_profits = [o["net_profit_after_fees"] for o in all_ops]
    print(f"  gross gap (cents):    "
          f"median ${statistics.median(gross_gaps):.3f}  "
          f"max ${max(gross_gaps):.3f}  "
          f"P95 ${sorted(gross_gaps)[int(0.95*len(gross_gaps))]:.3f}")
    print(f"  net after fees:       "
          f"median ${statistics.median(net_profits):+.3f}  "
          f"max ${max(net_profits):+.3f}")
    profitable_after_fees = sum(1 for x in net_profits if x > 0)
    print(f"  profitable after fees: {profitable_after_fees} / {len(all_ops)} "
          f"({100*profitable_after_fees/len(all_ops):.1f}%)")

    # Per-event summary
    by_event = defaultdict(list)
    for o in all_ops:
        by_event[o["event_slug"]].append(o)
    summary_rows = []
    for ev, ops in sorted(by_event.items(), key=lambda x: -len(x[1])):
        n = len(ops)
        max_gap = max(o["gross_gap"] for o in ops)
        max_net = max(o["net_profit_after_fees"] for o in ops)
        n_net_pos = sum(1 for o in ops if o["net_profit_after_fees"] > 0)
        summary_rows.append({
            "event_slug": ev, "n_flagged": n,
            "max_gross_gap": round(max_gap, 4),
            "max_net_profit": round(max_net, 4),
            "n_profitable_after_fees": n_net_pos,
        })

    print()
    print(f"=== per-event (top 8 by flagged count) ===")
    print(f"  {'event':<42s}  {'n':>5s}  {'max_gap':>8s}  {'max_net':>8s}  {'>0_after_fees':>14s}")
    for r in summary_rows[:8]:
        print(f"  {r['event_slug'][:42]:<42s}  {r['n_flagged']:>5d}  "
              f"${r['max_gross_gap']:>7.3f}  ${r['max_net_profit']:>+7.3f}  "
              f"{r['n_profitable_after_fees']:>14d}")
    with (out_dir / "arb_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)
    print(f"wrote {out_dir / 'arb_summary.csv'}")

    # Plot: gap distribution
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.hist(gross_gaps, bins=50, color="#2563eb", alpha=0.75,
            label=f"gross gap (n={len(gross_gaps)})")
    ax.hist(net_profits, bins=50, color="#dc2626", alpha=0.5,
            label="net after fees")
    ax.axvline(0, color="#111827", lw=1, ls="--")
    ax.axvline(5 * PER_SIDE_COST, color="#9ca3af", lw=1, ls=":",
               label=f"fee floor (N=5)  ${5*PER_SIDE_COST:.2f}")
    ax.set_xlabel("USD per arb (gross or net)")
    ax.set_ylabel("flagged moments")
    ax.set_title(f"Cross-bracket arb scanner — gap distribution\n"
                 f"{len(all_ops)} moments flagged, "
                 f"{profitable_after_fees} profitable net of fees")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "arb_gap_distribution.png", dpi=160)
    plt.close(fig)
    print(f"wrote {out_dir / 'arb_gap_distribution.png'}")

    # Verdict
    print()
    if profitable_after_fees == 0:
        verdict = ("NO arb edge detected. After fees+spread, no flagged moment "
                   "is profitable. Polymarket bracket prices are internally "
                   "consistent enough at this granularity that an arb scanner "
                   "is not a path to edge.")
    elif profitable_after_fees < 0.05 * len(all_ops):
        verdict = (f"WEAK arb signal. Only {profitable_after_fees} of {len(all_ops)} "
                   f"flagged moments are net-profitable. Too sparse to be a "
                   f"reliable strategy on its own.")
    else:
        verdict = (f"REAL arb signal. {profitable_after_fees} of {len(all_ops)} "
                   f"flagged moments are net-profitable after fees. Worth "
                   f"building a live executor.")
    print(f"VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
