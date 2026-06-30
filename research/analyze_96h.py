"""End-of-experiment analysis for the 96h run.

Reads:
  - The latest live_competitor_test run directory.
  - All snapshots under data/experiments/96h_snapshots/

Produces:
  - data/experiments/96h_report/REPORT.md     (human-readable summary)
  - data/experiments/96h_report/winners.csv   (top 30 by realized P&L)
  - data/experiments/96h_report/calibration.csv
  - data/experiments/96h_report/timeseries.csv  (pnl_summary over time)
  - notebooks/png/96h_*.png                   (plots overlayed in your usual dir)

Usage from repo root:
    python -m research.analyze_96h
"""

from __future__ import annotations

import csv
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, quantiles

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ROOT = Path(".")
SNAP_DIR = ROOT / "data" / "experiments" / "96h_snapshots"
REPORT_DIR = ROOT / "data" / "experiments" / "96h_report"
PLOT_DIR = ROOT / "notebooks" / "png"
LIVE_TESTS_DIR = ROOT / "data" / "reports" / "live_competitor_tests"


def latest_run() -> Path | None:
    if not LIVE_TESTS_DIR.exists():
        return None
    runs = sorted(LIVE_TESTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None


def load_config(cid: str, run_dir: Path) -> dict:
    """Find the JSON params for a competitor in the configs glob used."""
    candidates = list(Path("data/experiments/96h_pool").glob(f"{cid}.json"))
    if not candidates:
        return {}
    try:
        d = json.loads(candidates[0].read_text())
        return d.get("params") or {}
    except Exception:
        return {}


def read_trades(run_dir: Path) -> tuple[list[dict], list[dict]]:
    buys, sells = [], []
    for fp in (run_dir / "competitors").glob("*/trades.jsonl"):
        cid = fp.parent.name
        try:
            with fp.open() as f:
                for line in f:
                    try:
                        t = json.loads(line)
                    except Exception:
                        continue
                    t["_cid"] = cid
                    if t.get("action") == "BUY":
                        buys.append(t)
                    elif t.get("action") == "SELL":
                        sells.append(t)
        except Exception:
            pass
    return buys, sells


def write_winners_csv(rows: list[dict], out: Path) -> None:
    fields = [
        "competitor_id", "total_pnl", "equity", "open_positions",
        "entry_edge", "take_profit", "stop_loss", "nb_phi",
        "rate_prior_strength_h", "hawkes_alpha", "uncertainty",
        "risk_per_trade_pct", "spread_floor_multiplier",
        "signal_flip_min_edge_gap", "max_event_exposure_pct",
        "position_size_disagreement_penalty", "max_positions",
    ]
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in rows:
            w.writerow([r.get(k, "") for k in fields])


def correlate_params_with_pnl(rows: list[dict]) -> list[tuple[str, float, int]]:
    """For each param, Pearson r between value and total_pnl."""
    if not rows:
        return []
    out = []
    params = [
        "entry_edge", "take_profit", "stop_loss", "nb_phi",
        "rate_prior_strength_h", "hawkes_alpha", "uncertainty",
        "risk_per_trade_pct", "spread_floor_multiplier",
        "signal_flip_min_edge_gap", "max_event_exposure_pct",
        "position_size_disagreement_penalty", "max_positions",
    ]
    for p in params:
        vals = [(r[p], r["total_pnl"]) for r in rows if isinstance(r.get(p), (int, float))]
        if len(vals) < 5:
            continue
        xs = [v[0] for v in vals]
        ys = [v[1] for v in vals]
        mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
        num = sum((x-mx)*(y-my) for x, y in vals)
        denx = sum((x-mx)**2 for x in xs) ** 0.5
        deny = sum((y-my)**2 for y in ys) ** 0.5
        if denx == 0 or deny == 0:
            continue
        r = num / (denx * deny)
        out.append((p, r, len(vals)))
    out.sort(key=lambda x: -abs(x[1]))
    return out


def calibration_table(buys: list[dict], sells: list[dict]) -> list[dict]:
    """Realized win rate vs predicted prob_make_money bucketed by decile."""
    buys_by_key = {(b["_cid"], b["event_slug"], b["label"], b["side"]): b for b in buys}
    buckets: dict[float, dict] = {}
    for s in sells:
        k = (s["_cid"], s["event_slug"], s["label"], s["side"])
        b = buys_by_key.get(k)
        if b is None:
            continue
        pred = float(b.get("prob_make_money") or 0)
        bucket = round(pred * 10) / 10
        d = buckets.setdefault(bucket, {"n": 0, "wins": 0, "sum_pred": 0.0, "sum_pnl": 0.0})
        d["n"] += 1
        d["sum_pred"] += pred
        d["sum_pnl"] += float(s["pnl"])
        if float(s["pnl"]) > 0:
            d["wins"] += 1
    out = []
    for k in sorted(buckets):
        v = buckets[k]
        out.append({
            "pred_decile": k,
            "n": v["n"],
            "pred_mean": v["sum_pred"]/v["n"],
            "realized_win_rate": v["wins"]/v["n"],
            "mean_pnl": v["sum_pnl"]/v["n"],
        })
    return out


def load_snapshots() -> list[dict]:
    out = []
    if not SNAP_DIR.exists():
        return out
    for fp in sorted(SNAP_DIR.glob("snapshot_*.json")):
        try:
            out.append(json.loads(fp.read_text()))
        except Exception:
            pass
    return out


def plot_pnl_trajectory(snapshots: list[dict], plot_path: Path) -> None:
    if not snapshots:
        return
    times = []
    medians = []
    mins = []
    maxs = []
    profitables = []
    for s in snapshots:
        try:
            t = datetime.fromisoformat(s["ts"].replace("Z", "+00:00"))
            summ = s.get("summary") or {}
            times.append(t)
            medians.append(float(summ.get("median") or 0))
            mins.append(float(summ.get("min") or 0))
            maxs.append(float(summ.get("max") or 0))
            profitables.append(int(summ.get("profitable") or 0))
        except Exception:
            continue
    if not times:
        return
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    ax1.fill_between(times, mins, maxs, color="#94a3b8", alpha=0.3, label="min-max")
    ax1.plot(times, medians, color="#1d4ed8", lw=2.0, label="median P&L")
    ax1.axhline(0, color="black", lw=0.8)
    ax1.set_ylabel("competitor P&L ($)")
    ax1.set_title("96h experiment — P&L trajectory")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    ax2.plot(times, profitables, color="#16a34a", lw=2.0, label="profitable competitors")
    ax2.set_ylabel("count")
    ax2.set_xlabel("time (UTC)")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(plot_path, dpi=130)
    plt.close(fig)


def plot_calibration(table: list[dict], plot_path: Path) -> None:
    if not table:
        return
    fig, ax = plt.subplots(figsize=(8, 6))
    xs = [r["pred_mean"] for r in table]
    ys = [r["realized_win_rate"] for r in table]
    ns = [r["n"] for r in table]
    ax.plot([0, 1], [0, 1], color="black", ls="--", lw=1.0, label="perfect calibration")
    ax.scatter(xs, ys, s=[max(20, n*5) for n in ns], c=ys, cmap="RdYlGn", vmin=0, vmax=1,
               edgecolor="black", linewidth=0.5)
    for x, y, n in zip(xs, ys, ns):
        ax.annotate(f"n={n}", (x, y), xytext=(5, 5), textcoords="offset points", fontsize=8)
    ax.set_xlabel("predicted prob_make_money (bucket center)")
    ax.set_ylabel("realized win rate")
    ax.set_xlim(-0.05, 1.05)
    ax.set_ylim(-0.05, 1.05)
    ax.set_title("Calibration: predicted vs realized")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(plot_path, dpi=130)
    plt.close(fig)


def write_report(
    run_dir: Path, rows: list[dict], correlations: list[tuple[str, float, int]],
    cal: list[dict], snapshots: list[dict], out: Path,
) -> None:
    lines: list[str] = []
    lines.append(f"# 96h experiment report — generated {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append(f"Run dir: `{run_dir}`")
    lines.append(f"Snapshots collected: {len(snapshots)}")
    lines.append("")

    # leaderboard summary
    pnls = [r["total_pnl"] for r in rows]
    profitable = sum(1 for p in pnls if p > 0)
    lines.append("## Headline numbers")
    lines.append("")
    lines.append(f"- Competitors: **{len(rows)}**")
    lines.append(f"- Profitable: **{profitable}** ({profitable/len(rows)*100:.0f}%)" if rows else "")
    lines.append(f"- Mean P&L: ${sum(pnls)/len(pnls):+.2f}" if pnls else "")
    lines.append(f"- Median P&L: ${median(pnls):+.2f}" if pnls else "")
    lines.append(f"- Best: ${max(pnls):+.2f}" if pnls else "")
    lines.append(f"- Worst: ${min(pnls):+.2f}" if pnls else "")
    lines.append("")

    lines.append("## Top 15")
    lines.append("")
    lines.append("| competitor | P&L | open | entry_edge | take_profit | nb_phi | rate_prior | hawkes_alpha |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows[:15]:
        lines.append(
            f"| {r['competitor_id']} | {r['total_pnl']:+.2f} | {r['open_positions']} | "
            f"{r.get('entry_edge','')} | {r.get('take_profit','')} | {r.get('nb_phi','')} | "
            f"{r.get('rate_prior_strength_h','')} | {r.get('hawkes_alpha','')} |"
        )
    lines.append("")

    lines.append("## Bottom 10")
    lines.append("")
    lines.append("| competitor | P&L | open | entry_edge | take_profit | nb_phi |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows[-10:]:
        lines.append(
            f"| {r['competitor_id']} | {r['total_pnl']:+.2f} | {r['open_positions']} | "
            f"{r.get('entry_edge','')} | {r.get('take_profit','')} | {r.get('nb_phi','')} |"
        )
    lines.append("")

    lines.append("## Parameter ↔ P&L correlations")
    lines.append("")
    lines.append("Strong positive r = higher values of this param correlated with better P&L.")
    lines.append("")
    lines.append("| param | Pearson r | n |")
    lines.append("|---|---|---|")
    for p, r, n in correlations[:15]:
        lines.append(f"| {p} | {r:+.3f} | {n} |")
    lines.append("")

    lines.append("## Calibration (predicted prob_make_money vs realized win rate)")
    lines.append("")
    lines.append("| pred decile | n | pred_mean | realized_win | mean_pnl |")
    lines.append("|---|---|---|---|---|")
    for c in cal:
        lines.append(
            f"| {c['pred_decile']:.1f} | {c['n']} | {c['pred_mean']:.3f} | "
            f"{c['realized_win_rate']:.3f} | {c['mean_pnl']:+.2f} |"
        )
    lines.append("")

    # Anchor performance
    anchors = [r for r in rows if r["competitor_id"].startswith("anchor_")]
    if anchors:
        lines.append("## Anchor performance")
        lines.append("")
        lines.append("| anchor | P&L | rank |")
        lines.append("|---|---|---|")
        for a in anchors:
            rank = next((i+1 for i, r in enumerate(rows) if r["competitor_id"] == a["competitor_id"]), -1)
            lines.append(f"| {a['competitor_id']} | {a['total_pnl']:+.2f} | {rank}/{len(rows)} |")
        lines.append("")

    lines.append("## Plots")
    lines.append("")
    lines.append("- `notebooks/png/96h_pnl_trajectory.png` — P&L over time (median, min-max band)")
    lines.append("- `notebooks/png/96h_calibration.png` — predicted vs realized scatter")
    lines.append("- `notebooks/png/live_pnl_*` etc. — generated by `research.refresh_plots`")
    lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    rd = latest_run()
    if rd is None:
        print("ERROR: no run dir found under data/reports/live_competitor_tests/", file=sys.stderr)
        return 2
    print(f"Analyzing run: {rd}")

    # Orphan correction: positions on events that dropped off the price feed
    # are mis-valued at cost basis. leaderboard_corrected.csv (from
    # research.resolve_orphans) carries the true settled value.
    orphan_corr: dict[str, float] = {}
    corr_path = rd / "leaderboard_corrected.csv"
    if corr_path.exists():
        with corr_path.open() as f:
            for r in csv.DictReader(f):
                cid = r.get("competitor_id")
                d = float(r.get("orphan_correction") or 0.0)
                if cid and d:
                    orphan_corr[cid] = d
        print(f"  applying orphan corrections for {len(orphan_corr)} competitors "
              f"(+${sum(orphan_corr.values()):,.2f} total)")

    # leaderboard with params merged
    lb_path = rd / "leaderboard_latest.csv"
    rows: list[dict] = []
    if lb_path.exists():
        with lb_path.open() as f:
            for r in csv.DictReader(f):
                cid = r.get("competitor_id") or "?"
                params = load_config(cid, rd)
                delta = orphan_corr.get(cid, 0.0)
                merged = {
                    "competitor_id": cid,
                    "total_pnl": float(r.get("total_pnl") or 0) + delta,
                    "equity": float(r.get("equity") or 0) + delta,
                    "open_positions": int(float(r.get("open_positions") or 0)),
                }
                for k in (
                    "entry_edge", "take_profit", "stop_loss", "nb_phi",
                    "rate_prior_strength_h", "hawkes_alpha", "uncertainty",
                    "risk_per_trade_pct", "spread_floor_multiplier",
                    "signal_flip_min_edge_gap", "max_event_exposure_pct",
                    "position_size_disagreement_penalty", "max_positions",
                ):
                    v = params.get(k)
                    merged[k] = v
                rows.append(merged)
    rows.sort(key=lambda x: -x["total_pnl"])

    if not rows:
        print("ERROR: empty leaderboard", file=sys.stderr)
        return 2

    write_winners_csv(rows[:30], REPORT_DIR / "winners.csv")
    print(f"wrote {REPORT_DIR / 'winners.csv'}")

    correlations = correlate_params_with_pnl(rows)

    print("Reading trades for calibration table...")
    buys, sells = read_trades(rd)
    print(f"  {len(buys)} BUYs, {len(sells)} SELLs")

    cal = calibration_table(buys, sells)
    with (REPORT_DIR / "calibration.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pred_decile", "n", "pred_mean", "realized_win_rate", "mean_pnl"])
        for c in cal:
            w.writerow([f"{c['pred_decile']:.1f}", c["n"],
                       f"{c['pred_mean']:.4f}", f"{c['realized_win_rate']:.4f}",
                       f"{c['mean_pnl']:+.3f}"])
    print(f"wrote {REPORT_DIR / 'calibration.csv'}")

    snapshots = load_snapshots()

    # timeseries CSV
    with (REPORT_DIR / "timeseries.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "competitors", "profitable", "median_pnl", "min_pnl", "max_pnl", "buys", "sells", "win_rate"])
        for s in snapshots:
            summ = s.get("summary") or {}
            cal0 = s.get("calibration") or {}
            w.writerow([
                s.get("ts", ""),
                s.get("n_competitors", ""),
                summ.get("profitable", ""),
                summ.get("median", ""),
                summ.get("min", ""),
                summ.get("max", ""),
                cal0.get("buys", ""),
                cal0.get("sells", ""),
                cal0.get("win_rate", ""),
            ])

    plot_pnl_trajectory(snapshots, PLOT_DIR / "96h_pnl_trajectory.png")
    plot_calibration(cal, PLOT_DIR / "96h_calibration.png")
    print(f"wrote {PLOT_DIR / '96h_pnl_trajectory.png'}")
    print(f"wrote {PLOT_DIR / '96h_calibration.png'}")

    write_report(rd, rows, correlations, cal, snapshots, REPORT_DIR / "REPORT.md")
    print(f"wrote {REPORT_DIR / 'REPORT.md'}")
    print()
    print("=== top 5 ===")
    for r in rows[:5]:
        print(f"  {r['competitor_id']:24s} P&L={r['total_pnl']:+8.2f}  "
              f"entry_edge={r.get('entry_edge')}  take_profit={r.get('take_profit')}  "
              f"nb_phi={r.get('nb_phi')}")
    print()
    print(f"Full report: {REPORT_DIR / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
