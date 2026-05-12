"""Plot tweet-detection latency from `signals.latency_bench` output.

Reads detections.jsonl and renders:
  - per-method latency histogram         latency_hist.png
  - per-tweet method-by-method scatter    per_tweet_scatter.png
  - first-to-detect win bars + margins    first_to_detect.png
  - summary CSV                            latency_summary.csv

Usage:
  python -m research.plot_signal_latency
  python -m research.plot_signal_latency --in-dir data/signal_latency \
      --out-dir data/signal_latency/plots
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import mean, median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

METHOD_COLORS = {
    "direct_sse": "#dc2626",
    "gql_user_tweets": "#2563eb",
    "gql_user_by_id": "#16a34a",
    "browser_sse": "#a16207",
    "search_poll": "#7c3aed",
    "notifications_poll": "#db2777",
}
METHOD_ORDER_HINT = [
    "direct_sse",
    "gql_user_tweets",
    "gql_user_by_id",
    "browser_sse",
    "search_poll",
    "notifications_poll",
]


def load_detections(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with path.open() as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            if "tweet_id" in obj and "method" in obj:
                out.append(obj)
    return out


def order_methods(methods: set[str]) -> list[str]:
    ordered = [m for m in METHOD_ORDER_HINT if m in methods]
    extras = sorted(m for m in methods if m not in ordered)
    return ordered + extras


def plot_hist(detections: list[dict], out: Path):
    by_method: dict[str, list[float]] = {}
    for d in detections:
        lat = d.get("latency_ms")
        if isinstance(lat, (int, float)):
            by_method.setdefault(d["method"], []).append(float(lat))
    methods = order_methods(set(by_method))
    if not methods:
        return
    fig, ax = plt.subplots(figsize=(10, 6))
    all_vals = [v for vs in by_method.values() for v in vs]
    if not all_vals:
        return
    p99 = sorted(all_vals)[max(0, int(len(all_vals) * 0.99) - 1)]
    upper = max(500.0, min(10_000.0, p99 * 1.2))
    bins = list(range(0, int(upper) + 100, 100))
    for m in methods:
        vals = [v for v in by_method[m] if v <= upper]
        if not vals:
            continue
        ax.hist(
            vals,
            bins=bins,
            alpha=0.55,
            label=f"{m} (n={len(by_method[m])}, med={median(by_method[m]):.0f}ms)",
            color=METHOD_COLORS.get(m),
            edgecolor="black",
            linewidth=0.4,
        )
    ax.set_xlabel("latency since tweet creation (ms, snowflake)")
    ax.set_ylabel("count")
    ax.set_title("Tweet detection latency by method")
    ax.legend(loc="upper right")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)


def plot_per_tweet(detections: list[dict], out: Path):
    by_tweet: dict[str, dict[str, dict]] = {}
    for d in detections:
        by_tweet.setdefault(d["tweet_id"], {})[d["method"]] = d
    multi = {tid: ms for tid, ms in by_tweet.items() if len(ms) >= 1}
    if not multi:
        return
    tweets_sorted = sorted(
        multi.keys(),
        key=lambda t: min(d.get("detected_at", float("inf")) for d in multi[t].values()),
    )
    methods = order_methods({m for ms in multi.values() for m in ms})
    method_idx = {m: i for i, m in enumerate(methods)}
    fig, ax = plt.subplots(figsize=(11, max(4, 0.18 * len(tweets_sorted) + 2)))
    yticks: list[float] = []
    ylabels: list[str] = []
    for row, tid in enumerate(tweets_sorted):
        snow = None
        for d in multi[tid].values():
            snow = d.get("snowflake_unix")
            if snow is not None:
                break
        for m, d in multi[tid].items():
            lat = d.get("latency_ms")
            if not isinstance(lat, (int, float)):
                continue
            ax.scatter(
                lat,
                row,
                s=60,
                color=METHOD_COLORS.get(m, "#444"),
                edgecolor="black",
                linewidths=0.4,
                label=m,
            )
        yticks.append(row)
        ylabels.append(tid[-6:])
    handles, labels = ax.get_legend_handles_labels()
    by_label: dict = {}
    for h, l in zip(handles, labels):
        by_label[l] = h
    ordered_labels = [m for m in methods if m in by_label]
    ax.legend(
        [by_label[l] for l in ordered_labels],
        ordered_labels,
        loc="upper right",
    )
    ax.set_yticks(yticks)
    ax.set_yticklabels(ylabels, fontsize=7)
    ax.set_xlabel("latency since tweet creation (ms)")
    ax.set_title("Per-tweet detection latency by method")
    ax.grid(alpha=0.3, axis="x")
    ax.set_xscale("symlog", linthresh=200)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)


def plot_first_to_detect(detections: list[dict], out: Path):
    by_tweet: dict[str, dict[str, dict]] = {}
    for d in detections:
        by_tweet.setdefault(d["tweet_id"], {})[d["method"]] = d
    wins: dict[str, int] = {}
    margins: dict[str, list[float]] = {}
    for tid, methods in by_tweet.items():
        if len(methods) < 2:
            continue
        ordered = sorted(
            methods.items(),
            key=lambda kv: kv[1].get("detected_at", float("inf")),
        )
        winner_m, winner_d = ordered[0]
        runner_m, runner_d = ordered[1]
        wins[winner_m] = wins.get(winner_m, 0) + 1
        delta_ms = (runner_d["detected_at"] - winner_d["detected_at"]) * 1000.0
        margins.setdefault(winner_m, []).append(delta_ms)
    if not wins:
        return
    methods = order_methods(set(wins))
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    ax = axes[0]
    counts = [wins[m] for m in methods]
    colors = [METHOD_COLORS.get(m, "#444") for m in methods]
    ax.bar(methods, counts, color=colors, edgecolor="black", linewidth=0.5)
    ax.set_title("First-to-detect wins (only tweets seen by ≥2 methods)")
    ax.set_ylabel("wins")
    for i, c in enumerate(counts):
        ax.text(i, c, str(c), ha="center", va="bottom")
    ax.grid(alpha=0.3, axis="y")

    ax2 = axes[1]
    rows = [(m, margins.get(m, [])) for m in methods]
    rows = [(m, vs) for m, vs in rows if vs]
    if rows:
        ax2.boxplot(
            [vs for _, vs in rows],
            tick_labels=[m for m, _ in rows],
            patch_artist=True,
            boxprops=dict(facecolor="#e5e7eb"),
        )
        ax2.set_yscale("symlog", linthresh=100)
        ax2.set_ylabel("margin over runner-up (ms, log)")
        ax2.set_title("How much each winner beat the next-best method")
        ax2.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)


def write_summary_csv(detections: list[dict], out: Path):
    by_method: dict[str, list[float]] = {}
    by_tweet: dict[str, set[str]] = {}
    for d in detections:
        lat = d.get("latency_ms")
        if isinstance(lat, (int, float)):
            by_method.setdefault(d["method"], []).append(float(lat))
        by_tweet.setdefault(d["tweet_id"], set()).add(d["method"])
    methods = order_methods(set(by_method))
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "method", "detections", "lat_ms_mean", "lat_ms_p50",
            "lat_ms_p95", "lat_ms_min", "lat_ms_max",
        ])
        for m in methods:
            vals = sorted(by_method.get(m, []))
            n = len(vals)
            if not vals:
                w.writerow([m, 0, "", "", "", "", ""])
                continue
            p95 = vals[max(0, int(n * 0.95) - 1)]
            w.writerow([
                m, n,
                f"{mean(vals):.1f}", f"{median(vals):.1f}",
                f"{p95:.1f}", f"{min(vals):.1f}", f"{max(vals):.1f}",
            ])


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--in-dir", default="data/signal_latency")
    p.add_argument("--out-dir", default=None,
                   help="Defaults to <in-dir>/plots")
    args = p.parse_args(argv)

    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir) if args.out_dir else in_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    detections = load_detections(in_dir / "detections.jsonl")
    if not detections:
        print(f"No detections found in {in_dir / 'detections.jsonl'} yet.")
        return 1

    plot_hist(detections, out_dir / "latency_hist.png")
    plot_per_tweet(detections, out_dir / "per_tweet_scatter.png")
    plot_first_to_detect(detections, out_dir / "first_to_detect.png")
    write_summary_csv(detections, out_dir / "latency_summary.csv")

    print(f"Wrote plots to {out_dir}")
    print(f"  - latency_hist.png  ({len(detections)} detection rows)")
    print(f"  - per_tweet_scatter.png")
    print(f"  - first_to_detect.png")
    print(f"  - latency_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
