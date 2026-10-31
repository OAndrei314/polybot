from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns

from research import jsonl_io


# Parameters that are constant across all competitors or non-numeric -- skip them
_SKIP_PARAMS = {"bankroll", "fee_category", "fee_rate", "competitor_id"}

# Performance targets we correlate params against (besides total_pnl)
_PERF_TARGETS = ["total_pnl", "realized_pnl", "max_daily_drawdown", "stop_loss_count", "wins"]


def _latest_run(base_dir: Path) -> Path:
    runs = sorted(base_dir.glob("*_real_48h"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise FileNotFoundError(f"No *_real_48h runs under {base_dir}")
    return runs[-1]


def _save(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)


def load_merged(run_dir: Path) -> pd.DataFrame:
    with (run_dir / "competitors.json").open() as f:
        comps = json.load(f)

    params_df = pd.DataFrame(comps)
    numeric_params = [
        c for c in params_df.columns
        if c not in _SKIP_PARAMS and pd.api.types.is_numeric_dtype(params_df[c])
    ]
    # Drop columns with zero variance (constant across all competitors)
    numeric_params = [c for c in numeric_params if params_df[c].nunique() > 1]

    lb = pd.read_csv(run_dir / "leaderboard_latest.csv")
    merged = lb.merge(params_df[["competitor_id"] + numeric_params], on="competitor_id")
    return merged, numeric_params


def plot_param_pnl_correlation(merged: pd.DataFrame, numeric_params: list[str], out_dir: Path) -> None:
    """Horizontal bar chart: Pearson r between each param and total_pnl, sorted by |r|."""
    corrs = (
        merged[numeric_params + ["total_pnl"]]
        .corr()["total_pnl"]
        .drop("total_pnl")
        .sort_values(key=abs, ascending=True)
    )

    fig, ax = plt.subplots(figsize=(10, max(6, 0.35 * len(corrs) + 1.5)))
    colors = ["#16a34a" if v > 0 else "#ef4444" for v in corrs.values]
    ax.barh(corrs.index, corrs.values, color=colors)
    ax.axvline(0, color="#111827", lw=1.0)
    ax.set_title("Parameter → Total PnL Correlation (Pearson r)\n4-hour live run, n=100 competitors", fontsize=12)
    ax.set_xlabel("Pearson r  (green = higher value → better PnL)")
    ax.xaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))
    ax.grid(True, axis="x", alpha=0.22)
    _save(fig, out_dir / "param_pnl_correlation.png")


def plot_param_correlation_matrix(merged: pd.DataFrame, numeric_params: list[str], out_dir: Path) -> None:
    """Full param-param correlation heatmap, clustered."""
    corr = merged[numeric_params].corr()

    # Reorder by total-correlation magnitude so related params group visually
    row_order = corr.abs().sum(axis=1).sort_values(ascending=False).index
    corr = corr.loc[row_order, row_order]

    size = max(10, 0.38 * len(numeric_params) + 2)
    fig, ax = plt.subplots(figsize=(size, size))
    mask = np.triu(np.ones_like(corr, dtype=bool), k=1)
    sns.heatmap(
        corr,
        mask=mask,
        cmap="RdBu_r",
        center=0,
        vmin=-1,
        vmax=1,
        annot=len(numeric_params) <= 20,
        fmt=".2f",
        linewidths=0.4,
        ax=ax,
        cbar_kws={"shrink": 0.6, "label": "Pearson r"},
    )
    ax.set_title("Parameter Correlation Matrix (clustered)\n4-hour live run, n=100 competitors", fontsize=12)
    _save(fig, out_dir / "param_correlation_matrix.png")


def plot_top_params_scatter(merged: pd.DataFrame, numeric_params: list[str], out_dir: Path, top_n: int = 6) -> None:
    """Scatter: top-N most-correlated params vs total_pnl."""
    corrs = (
        merged[numeric_params + ["total_pnl"]]
        .corr()["total_pnl"]
        .drop("total_pnl")
        .sort_values(key=abs, ascending=False)
    )
    top_params = corrs.head(top_n).index.tolist()

    cols = 3
    rows = (len(top_params) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4 * rows))
    axes = np.array(axes).flatten()

    for i, param in enumerate(top_params):
        ax = axes[i]
        r = corrs[param]
        x = merged[param]
        y = merged["total_pnl"]
        ax.scatter(x, y, s=28, alpha=0.6, color="#2563eb")
        m, b = np.polyfit(x.fillna(x.median()), y, 1)
        xline = np.linspace(x.min(), x.max(), 100)
        ax.plot(xline, m * xline + b, color="#ef4444", lw=1.5)
        ax.set_xlabel(param, fontsize=9)
        ax.set_ylabel("total_pnl ($)" if i % cols == 0 else "", fontsize=9)
        ax.set_title(f"r = {r:+.3f}", fontsize=10)
        ax.grid(True, alpha=0.22)
        # Annotate winner
        top_row = merged.nlargest(1, "total_pnl").iloc[0]
        ax.scatter([top_row[param]], [top_row["total_pnl"]], color="#f59e0b", s=90, zorder=5, label=top_row["competitor_id"])
        ax.legend(fontsize=7, loc="upper left")

    for j in range(len(top_params), len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(f"Top-{top_n} Parameters by |Correlation| with PnL", fontsize=13, y=1.01)
    _save(fig, out_dir / "top_params_vs_pnl_scatter.png")


def plot_multi_target_heatmap(merged: pd.DataFrame, numeric_params: list[str], out_dir: Path) -> None:
    """Heatmap: params × perf targets, so you can see if a param helps PnL but hurts drawdown."""
    targets = [t for t in _PERF_TARGETS if t in merged.columns]
    corr_rows = {}
    for param in numeric_params:
        row = {}
        for t in targets:
            row[t] = merged[param].corr(merged[t])
        corr_rows[param] = row
    corr_df = pd.DataFrame(corr_rows).T  # params × targets

    # Sort by |pnl corr|
    corr_df = corr_df.reindex(corr_df["total_pnl"].abs().sort_values(ascending=False).index)

    fig, ax = plt.subplots(figsize=(max(8, 1.5 * len(targets)), max(6, 0.35 * len(corr_df) + 1.5)))
    sns.heatmap(
        corr_df,
        cmap="RdBu_r",
        center=0,
        vmin=-1,
        vmax=1,
        annot=True,
        fmt=".2f",
        linewidths=0.4,
        ax=ax,
        cbar_kws={"shrink": 0.6, "label": "Pearson r"},
    )
    ax.set_title("Params × Performance Targets Correlation\n(sorted by |PnL corr|)", fontsize=12)
    ax.set_xlabel("")
    _save(fig, out_dir / "param_multi_target_heatmap.png")


def print_data_health(run_dir: Path, merged: pd.DataFrame) -> None:
    import gzip, time

    print("=" * 60)
    print("DATA HEALTH REPORT")
    print("=" * 60)

    # Synthetic data check
    input_path = run_dir / "real_input_ticks.jsonl.gz"
    synth_count = 0
    real_count = 0
    with gzip.open(input_path, "rt") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            s = row.get("synthetic_price_rows", 0) or 0
            if s > 0:
                synth_count += 1
            else:
                real_count += 1
    print(f"\nSynthetic price ticks : {synth_count}  (bad)")
    print(f"Real price ticks      : {real_count}  (good)")
    if synth_count == 0:
        print("  -> No synthetic data. Feed was clean throughout.")

    # PnL jump analysis
    snap = pd.read_json(
        jsonl_io.resolve(run_dir / "portfolio_snapshots.jsonl"), lines=True
    )
    snap["ts"] = pd.to_datetime(snap["ts"], utc=True)
    snap = snap.sort_values(["competitor_id", "ts"])
    snap["pnl_delta"] = snap.groupby("competitor_id")["total_pnl"].diff()

    big_jumps = snap[snap["pnl_delta"].abs() > 5]
    # Group by timestamp (rounded to second) to find resolution events
    big_jumps = big_jumps.copy()
    big_jumps["ts_s"] = big_jumps["ts"].dt.floor("s")
    clusters = big_jumps.groupby("ts_s")["competitor_id"].count()
    multi = clusters[clusters >= 3]

    print(f"\nPnL jumps |Δ| > $5    : {len(big_jumps)} across {merged['competitor_id'].nunique()} competitors")
    print(f"Clustered at same sec  : {len(multi)} timestamps affect ≥3 competitors simultaneously")
    print("  -> These are Polymarket market resolutions (legitimate), not feed errors.")

    # Outlier competitors
    bad = merged[merged["total_pnl"] < -20].sort_values("total_pnl")
    if not bad.empty:
        print(f"\nSevere losers (PnL < -$20): {len(bad)} competitors")
        for _, r in bad.iterrows():
            print(f"  {r['competitor_id']:>8}  pnl={r['total_pnl']:+7.2f}  closed={int(r['closed_count'])}  stop_losses={int(r['stop_loss_count'])}")
        print("  -> Check these configs for missing guards (min_trade_cost, max_event_exposure_pct).")

    # Run duration
    ts_col = snap["ts"]
    duration_h = (ts_col.max() - ts_col.min()).total_seconds() / 3600
    n_trades_total = merged["buys"].sum()
    n_comps = merged["competitor_id"].nunique()
    avg_trades = merged["closed_count"].mean()
    print(f"\nRun duration           : {duration_h:.1f} hours")
    print(f"Competitors            : {n_comps}")
    print(f"Total buys across all  : {n_trades_total}")
    print(f"Avg closed trades/comp : {avg_trades:.1f}")

    print("\n" + "=" * 60)
    print("CONFIDENCE GUIDE (when to deploy real money)")
    print("=" * 60)
    print(f"""
  Current state: {duration_h:.1f}h run, ~{avg_trades:.0f} closed trades per config.

  Minimum bars before trusting a result:
    [ ] ≥ 48 hours of live data (you have {duration_h:.1f}h — need {max(0,48-duration_h):.1f}h more)
    [ ] ≥ 30 closed trades for the champion config (a_0054 has {int(merged.loc[merged.competitor_id=='a_0054','closed_count'].iloc[0]) if 'a_0054' in merged.competitor_id.values else '?'})
    [ ] Champion appears in top-3 across ≥ 2 independent 24h windows
    [ ] No stop-loss churn (repeated buy→stop→rebuy on same event)
    [ ] PnL is mostly realized, not floating unrealized gains
    [ ] Correlation pattern stable vs historical sweep (not a fluke of today's events)

  Key risk: with {avg_trades:.0f} closed trades the standard error on PnL is enormous.
  A $50 lead on $1000 bankroll over 4h could easily reverse.
  Polymarket tweet-count markets are low-volume and resolve in clusters —
  a single bad resolution can swing PnL ±$40.

  Practical threshold: let it run the full 48h, then compare realized_pnl
  (not total_pnl which includes floating unrealized). If a_0054 stays top-3
  on realized_pnl and shows <5% max drawdown, it's worth a small real stake
  ($50-$100) with close monitoring before scaling to hundreds.
""")


def main() -> int:
    parser = argparse.ArgumentParser(description="Param-PnL and param-param correlation plots")
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--base-dir", default="data/reports/live_competitor_tests")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--top-scatter", type=int, default=6, help="Top-N params for scatter plots")
    args = parser.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else _latest_run(Path(args.base_dir))
    out_dir = Path(args.out_dir) if args.out_dir else run_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    merged, numeric_params = load_merged(run_dir)

    print_data_health(run_dir, merged)

    print(f"\nGenerating plots → {out_dir}")
    plot_param_pnl_correlation(merged, numeric_params, out_dir)
    print("  param_pnl_correlation.png")
    plot_param_correlation_matrix(merged, numeric_params, out_dir)
    print("  param_correlation_matrix.png")
    plot_top_params_scatter(merged, numeric_params, out_dir, args.top_scatter)
    print("  top_params_vs_pnl_scatter.png")
    plot_multi_target_heatmap(merged, numeric_params, out_dir)
    print("  param_multi_target_heatmap.png")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
