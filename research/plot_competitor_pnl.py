from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from research import jsonl_io


def _latest_run(base_dir: Path) -> Path:
    runs = sorted(base_dir.glob("*_real_48h"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise FileNotFoundError(f"No *_real_48h runs found under {base_dir}")
    return runs[-1]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with jsonl_io.open_text(path) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _style_time_axis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(True, alpha=0.22)
    ax.set_xlabel("UTC time")


def _save(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)


def load_snapshots(run_dir: Path) -> pd.DataFrame:
    path = run_dir / "portfolio_snapshots.jsonl"
    rows = _read_jsonl(path)
    if not rows:
        raise RuntimeError(f"No rows in {path}")
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    for col in [
        "cash",
        "invested",
        "unrealized_pnl",
        "realized_pnl",
        "equity",
        "total_pnl",
        "open_positions",
        "closed_count",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values(["competitor_id", "ts"])


def write_latest_summary(df: pd.DataFrame, out_dir: Path) -> pd.DataFrame:
    summaries = []
    for cid, g in df.groupby("competitor_id", sort=False):
        g = g.sort_values("ts")
        latest = g.iloc[-1].to_dict()
        equity = g["equity"].astype(float)
        pnl = g["total_pnl"].astype(float)
        dd = (equity / equity.cummax()) - 1.0
        summaries.append(
            {
                "competitor_id": cid,
                "ts": latest["ts"],
                "equity": latest["equity"],
                "total_pnl": latest["total_pnl"],
                "realized_pnl": latest["realized_pnl"],
                "unrealized_pnl": latest["unrealized_pnl"],
                "open_positions": latest["open_positions"],
                "closed_count": latest["closed_count"],
                "min_pnl": pnl.min(),
                "max_pnl": pnl.max(),
                "max_drawdown_pct": abs(dd.min()) * 100.0,
            }
        )
    summary = pd.DataFrame(summaries).sort_values("total_pnl", ascending=False)
    summary.to_csv(out_dir / "latest_summary.csv", index=False)
    return summary


def plot_all_pnl(df: pd.DataFrame, out_dir: Path) -> None:
    latest = df.groupby("competitor_id").tail(1).set_index("competitor_id")
    ordered = latest.sort_values("total_pnl", ascending=False).index.tolist()

    fig, ax = plt.subplots(figsize=(14, 8))
    for cid in ordered:
        g = df[df["competitor_id"] == cid]
        color = "#b7bcc5"
        lw = 1.0
        alpha = 0.55
        z = 1
        if cid == "baseline":
            color, lw, alpha, z = "#f59e0b", 2.4, 0.95, 4
        if cid == "b_0003":
            color, lw, alpha, z = "#2563eb", 3.0, 1.0, 5
        if cid == "b_0016":
            color, lw, alpha, z = "#dc2626", 3.0, 1.0, 6
        ax.plot(g["ts"], g["total_pnl"], label=cid, color=color, lw=lw, alpha=alpha, zorder=z)
    ax.axhline(0, color="#111827", lw=1.1, alpha=0.65)
    ax.set_title("Live Arena PnL: All Competitors")
    ax.set_ylabel("PnL on $1,000 paper bankroll ($)")
    _style_time_axis(ax)
    handles, labels = ax.get_legend_handles_labels()
    keep = [i for i, label in enumerate(labels) if label in {"b_0003", "baseline", "b_0016"}]
    if keep:
        ax.legend([handles[i] for i in keep], [labels[i] for i in keep], loc="best")
    _save(fig, out_dir / "live_pnl_all_competitors.png")


def plot_top_bottom(df: pd.DataFrame, out_dir: Path, n: int) -> None:
    latest = df.groupby("competitor_id").tail(1).sort_values("total_pnl", ascending=False)
    selected = list(dict.fromkeys(latest.head(n)["competitor_id"].tolist() + latest.tail(n)["competitor_id"].tolist()))
    colors = plt.cm.tab20.colors
    fig, ax = plt.subplots(figsize=(14, 8))
    for idx, cid in enumerate(selected):
        g = df[df["competitor_id"] == cid]
        lw = 3.0 if cid == "b_0003" else 1.8
        ax.plot(g["ts"], g["total_pnl"], label=cid, lw=lw, color=colors[idx % len(colors)])
    ax.axhline(0, color="#111827", lw=1.1, alpha=0.65)
    ax.set_title(f"Live Arena PnL: Top/Bottom {n} Competitors")
    ax.set_ylabel("PnL on $1,000 paper bankroll ($)")
    _style_time_axis(ax)
    ax.legend(ncol=2, fontsize=8)
    _save(fig, out_dir / "live_pnl_top_bottom.png")


def plot_latest_bar(summary: pd.DataFrame, out_dir: Path) -> None:
    fig_h = max(6, 0.34 * len(summary) + 1.5)
    fig, ax = plt.subplots(figsize=(12, fig_h))
    colors = ["#2563eb" if cid == "b_0003" else "#ef4444" if pnl < 0 else "#16a34a" for cid, pnl in zip(summary["competitor_id"], summary["total_pnl"])]
    ax.barh(summary["competitor_id"], summary["total_pnl"], color=colors)
    ax.axvline(0, color="#111827", lw=1.0)
    ax.invert_yaxis()
    ax.set_title("Latest Live PnL By Competitor")
    ax.set_xlabel("PnL on $1,000 paper bankroll ($)")
    ax.grid(True, axis="x", alpha=0.22)
    _save(fig, out_dir / "latest_pnl_bar.png")


def plot_drawdowns(df: pd.DataFrame, out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(14, 8))
    latest = df.groupby("competitor_id").tail(1).sort_values("total_pnl", ascending=False)
    for cid in latest["competitor_id"]:
        g = df[df["competitor_id"] == cid].copy()
        equity = g["equity"].astype(float)
        g["drawdown_pct"] = (equity / equity.cummax() - 1.0) * 100.0
        color = "#2563eb" if cid == "b_0003" else "#b7bcc5"
        lw = 2.8 if cid == "b_0003" else 1.0
        alpha = 1.0 if cid == "b_0003" else 0.55
        ax.plot(g["ts"], g["drawdown_pct"], color=color, lw=lw, alpha=alpha, label=cid if cid == "b_0003" else None)
    ax.axhline(0, color="#111827", lw=1.0, alpha=0.5)
    ax.set_title("Live Arena Drawdown From Running Equity High")
    ax.set_ylabel("Drawdown (%)")
    _style_time_axis(ax)
    ax.legend(loc="lower left")
    _save(fig, out_dir / "live_drawdowns_all_competitors.png")


def plot_heatmap(df: pd.DataFrame, out_dir: Path) -> None:
    tmp = df.copy()
    tmp["ts_bin"] = tmp["ts"].dt.floor("5min")
    pivot = (
        tmp.sort_values("ts")
        .groupby(["competitor_id", "ts_bin"])["total_pnl"]
        .last()
        .unstack("ts_bin")
    )
    order = pivot.iloc[:, -1].sort_values(ascending=False).index
    pivot = pivot.loc[order]
    fig, ax = plt.subplots(figsize=(14, max(6, 0.34 * len(pivot) + 1.5)))
    im = ax.imshow(pivot.values, aspect="auto", cmap="RdYlGn", interpolation="nearest")
    ax.set_title("Live Arena PnL Heatmap, 5-Minute Buckets")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)
    if len(pivot.columns):
        tick_idx = list(range(0, len(pivot.columns), max(1, len(pivot.columns) // 8)))
        ax.set_xticks(tick_idx)
        ax.set_xticklabels([pivot.columns[i].strftime("%H:%M") for i in tick_idx], rotation=35, ha="right")
    ax.set_xlabel("UTC time")
    fig.colorbar(im, ax=ax, label="PnL ($)")
    _save(fig, out_dir / "live_pnl_heatmap.png")


def plot_b003_bug_adjusted(df: pd.DataFrame, run_dir: Path, out_dir: Path) -> None:
    trade_path = run_dir / "competitors" / "b_0003" / "trades.jsonl"
    if not trade_path.exists():
        return
    bug_pnl = 0.0
    bug_ts = None
    for row in _read_jsonl(trade_path):
        if (
            row.get("action") == "SELL"
            and row.get("reason") == "edge_decay"
            and row.get("model_p_yes") is None
            and row.get("lambda_remaining") is None
        ):
            bug_pnl += abs(float(row.get("pnl") or 0.0))
            bug_ts = pd.to_datetime(row.get("ts"), utc=True)
    if bug_pnl <= 0 or bug_ts is None:
        return
    g = df[df["competitor_id"] == "b_0003"].copy().sort_values("ts")
    g["bug_adjusted_pnl"] = g["total_pnl"] + (g["ts"] >= bug_ts).astype(float) * bug_pnl
    fig, ax = plt.subplots(figsize=(14, 7))
    ax.plot(g["ts"], g["total_pnl"], label="reported b_0003", color="#2563eb", lw=2.5)
    ax.plot(g["ts"], g["bug_adjusted_pnl"], label=f"bug-adjusted (+${bug_pnl:.2f})", color="#16a34a", lw=2.5, ls="--")
    ax.axhline(0, color="#111827", lw=1.1, alpha=0.65)
    ax.axvline(bug_ts, color="#ef4444", lw=1.2, alpha=0.75, ls=":")
    ax.set_title("b_0003 Reported Vs Bug-Adjusted PnL")
    ax.set_ylabel("PnL on $1,000 paper bankroll ($)")
    _style_time_axis(ax)
    ax.legend()
    _save(fig, out_dir / "b003_reported_vs_bug_adjusted.png")


def plot_sweep(sweep_csv: Path, out_dir: Path) -> None:
    if not sweep_csv.exists():
        return
    rows: list[dict[str, Any]] = []
    with sweep_csv.open() as f:
        for row in csv.DictReader(f):
            rows.append(row)
    if not rows:
        return
    df = pd.DataFrame(rows)
    for col in ["net_pnl", "max_drawdown", "sharpe", "composite_score"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["max_drawdown_pct"] = df["max_drawdown"] * 100.0

    fig, ax = plt.subplots(figsize=(12, 8))
    colors = df["beats_baseline_guardrail"].map({"True": "#16a34a", "False": "#ef4444"}).fillna("#64748b")
    ax.scatter(df["max_drawdown_pct"], df["net_pnl"], s=80, c=colors, alpha=0.82)
    for _, row in df.iterrows():
        if row["competitor_id"] in {"b_0003", "baseline"}:
            ax.annotate(row["competitor_id"], (row["max_drawdown_pct"], row["net_pnl"]), xytext=(5, 5), textcoords="offset points")
    ax.set_title("Historical Sweep: Net PnL Vs Max Drawdown")
    ax.set_xlabel("Max drawdown (%)")
    ax.set_ylabel("Historical net PnL ($, paper model)")
    ax.grid(True, alpha=0.22)
    _save(fig, out_dir / "sweep_net_pnl_vs_drawdown.png")

    top = df.sort_values("composite_score", ascending=False).head(15).sort_values("composite_score")
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.barh(top["competitor_id"], top["composite_score"], color="#2563eb")
    ax.set_title("Historical Sweep: Top Composite Scores")
    ax.set_xlabel("Composite robustness score")
    ax.grid(True, axis="x", alpha=0.22)
    _save(fig, out_dir / "sweep_top_composite.png")


def plot_pnl_distribution(df: pd.DataFrame, run_dir: Path, out_dir: Path) -> None:
    """Two-panel distribution chart.

    Top panel  — histogram of final PnL coloured green/red showing how many
                 model variants are profitable, with median and mean markers.
    Bottom panel — scatter of total-$ bought (sum of all BUY costs) vs final PnL
                   so you can see whether more active bots did better or worse.
    """
    # Final PnL per competitor from snapshots.
    final = (
        df.sort_values("ts")
        .groupby("competitor_id")["total_pnl"]
        .last()
        .reset_index()
    )
    final.columns = ["competitor_id", "pnl"]

    # Total cash deployed: sum all BUY costs from each competitor's trade log.
    total_bought: dict[str, float] = {}
    comp_root = run_dir / "competitors"
    if comp_root.exists():
        for comp_dir in sorted(comp_root.iterdir()):
            cid = comp_dir.name
            trade_path = comp_dir / "trades.jsonl"
            if not trade_path.exists():
                total_bought[cid] = 0.0
                continue
            total = 0.0
            with trade_path.open() as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        if str(row.get("action", "")).upper() == "BUY":
                            total += float(row.get("cost") or 0.0)
                    except Exception:
                        pass
            total_bought[cid] = total

    final["total_bought"] = final["competitor_id"].map(total_bought).fillna(0.0)

    pnl = final["pnl"]
    n_total = len(pnl)
    n_pos = int((pnl > 0).sum())
    n_neg = int((pnl < 0).sum())
    n_zero = int((pnl == 0).sum())
    median_pnl = float(pnl.median())
    mean_pnl = float(pnl.mean())

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(13, 11), gridspec_kw={"height_ratios": [1.7, 1]}
    )

    # ── top: PnL histogram ───────────────────────────────────────────────────
    pnl_min, pnl_max = float(pnl.min()), float(pnl.max())
    bin_edges = [pnl_min + i * (pnl_max - pnl_min) / 25 for i in range(26)]

    neg_vals = pnl[pnl < 0].tolist()
    pos_vals = pnl[pnl >= 0].tolist()
    if neg_vals:
        ax_top.hist(neg_vals, bins=bin_edges, color="#ef4444", alpha=0.82,
                    label=f"Loss  ({n_neg})", zorder=2)
    if pos_vals:
        ax_top.hist(pos_vals, bins=bin_edges, color="#16a34a", alpha=0.82,
                    label=f"Profit ({n_pos})", zorder=2)

    # Optional KDE density curve (requires scipy; skipped silently if absent).
    try:
        from scipy.stats import gaussian_kde
        import numpy as _np
        kde = gaussian_kde(pnl.values, bw_method=0.35)
        xs = _np.linspace(pnl_min - 5, pnl_max + 5, 400)
        kde_scale = n_total * (pnl_max - pnl_min) / 25
        ax_top.plot(xs, kde(xs) * kde_scale, color="#1e40af", lw=2.2,
                    zorder=3, label="KDE density")
    except Exception:
        pass

    ax_top.axvline(0, color="#111827", lw=1.5, alpha=0.85, zorder=4)
    ax_top.axvline(median_pnl, color="#7c3aed", lw=1.6, ls="--", alpha=0.90,
                   zorder=4, label=f"Median  ${median_pnl:+.1f}")
    ax_top.axvline(mean_pnl, color="#0891b2", lw=1.6, ls=":", alpha=0.90,
                   zorder=4, label=f"Mean    ${mean_pnl:+.1f}")

    pct_pos = 100 * n_pos / max(n_total, 1)
    ax_top.set_title(
        f"PnL Distribution — {n_pos}/{n_total} profitable ({pct_pos:.0f}%)  ·  "
        f"{n_zero} flat  ·  {n_neg} loss",
        fontsize=12,
    )
    ax_top.set_xlabel("Final PnL on $1,000 paper bankroll ($)")
    ax_top.set_ylabel("Number of model variants")
    ax_top.legend(fontsize=9)
    ax_top.grid(True, axis="y", alpha=0.22)

    # ── bottom: total $ bought vs final PnL scatter ──────────────────────────
    colors_bot = ["#16a34a" if p >= 0 else "#ef4444" for p in final["pnl"]]
    ax_bot.scatter(final["total_bought"], final["pnl"],
                   c=colors_bot, s=55, alpha=0.78, zorder=3)
    ax_bot.axhline(0, color="#111827", lw=1.1, alpha=0.7)
    ax_bot.set_xlabel("Total $ deployed over run (sum of all BUY costs)")
    ax_bot.set_ylabel("Final PnL ($)")
    ax_bot.set_title("Activity vs Profitability — did trading more help?")
    ax_bot.grid(True, alpha=0.22)

    _save(fig, out_dir / "pnl_distribution.png")


def _render_once(args: argparse.Namespace, run_dir: Path, out_dir: Path) -> None:
    import time as _time
    t0 = _time.monotonic()
    df = load_snapshots(run_dir)
    summary = write_latest_summary(df, out_dir)
    plot_all_pnl(df, out_dir)
    plot_top_bottom(df, out_dir, args.top_bottom)
    plot_latest_bar(summary, out_dir)
    plot_drawdowns(df, out_dir)
    plot_heatmap(df, out_dir)
    plot_pnl_distribution(df, run_dir, out_dir)
    plot_b003_bug_adjusted(df, run_dir, out_dir)
    plot_sweep(Path(args.sweep_csv), out_dir)
    elapsed = _time.monotonic() - t0
    last_ts = df["ts"].max()
    print(f"[{_time.strftime('%H:%M:%S')}] rendered {len(df)} snapshots through {last_ts.strftime('%H:%M UTC')}  ({elapsed:.1f}s)")
    print("latest_top")
    for _, row in summary.head(8).iterrows():
        print(
            f"  {row['competitor_id']:>8} pnl={row['total_pnl']:+8.2f} "
            f"equity={row['equity']:8.2f} dd={row['max_drawdown_pct']:5.2f}%"
        )


def main() -> int:
    import time as _time
    parser = argparse.ArgumentParser(description="Plot live competitor PnL and sweep summary")
    parser.add_argument("--run-dir", default=None, help="Live run dir; defaults to latest under --base-dir")
    parser.add_argument("--base-dir", default="data/reports/live_competitor_tests")
    parser.add_argument("--sweep-csv", default="data/reports/sweeps/20260502_104223/leaderboard_dev_all_competitors.csv")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--top-bottom", type=int, default=6)
    parser.add_argument("--watch-s", type=float, default=0.0,
                        help="Re-render every N seconds (0 = run once). Example: --watch-s 300")
    args = parser.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else _latest_run(Path(args.base_dir))
    out_dir = Path(args.out_dir) if args.out_dir else run_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"run_dir={run_dir}")
    print(f"out_dir={out_dir}")

    _render_once(args, run_dir, out_dir)

    if args.watch_s > 0:
        print(f"\nWatching — re-rendering every {args.watch_s:.0f}s. Ctrl-C to stop.")
        while True:
            try:
                _time.sleep(args.watch_s)
            except KeyboardInterrupt:
                break
            _render_once(args, run_dir, out_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
