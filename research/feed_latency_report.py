from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


PRICE_KEYS = ("yes_buy", "yes_sell", "no_buy", "no_sell")


def _latest_run(base_dir: Path) -> Path:
    runs = sorted(base_dir.glob("*_real_48h"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise FileNotFoundError(f"No *_real_48h runs found under {base_dir}")
    return runs[-1]


def _read_gzip_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _price_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(k) for k in PRICE_KEYS)


def build_feed_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    prev_ts = None
    prev_prices: dict[tuple[str, str], tuple[Any, ...]] = {}
    for raw in rows:
        ts = pd.to_datetime(raw.get("ts"), utc=True)
        price_rows = raw.get("price_rows") or []
        cur_prices: dict[tuple[str, str], tuple[Any, ...]] = {}
        changed = 0
        inferred_missing_bbo = 0
        for row in price_rows:
            key = (str(row.get("event_slug") or ""), str(row.get("label") or ""))
            sig = _price_signature(row)
            cur_prices[key] = sig
            if prev_prices.get(key) != sig:
                changed += 1
            if row.get("is_synthetic_price") or row.get("yes_age_s") is None or row.get("no_age_s") is None:
                inferred_missing_bbo += 1

        state = raw.get("price_feed_state") or {}
        out.append(
            {
                "ts": ts,
                "gap_s": None if prev_ts is None else (ts - prev_ts).total_seconds(),
                "price_rows": len(price_rows),
                "changed_price_rows": changed,
                "synthetic_price_rows": raw.get("synthetic_price_rows"),
                "inferred_missing_bbo_rows": inferred_missing_bbo,
                "ws_connected": state.get("ws_connected"),
                "ws_ok": state.get("ws_ok"),
                "last_ws_event_age_s": state.get("last_ws_event_age_s"),
                "tracked_tokens": state.get("tracked_tokens"),
                "stale_tokens": state.get("stale_tokens"),
                "empty_tokens": state.get("empty_tokens"),
                "max_bbo_age_s": state.get("max_bbo_age_s"),
            }
        )
        prev_ts = ts
        prev_prices = cur_prices
    return pd.DataFrame(out)


def build_loop_frame(run_dir: Path) -> pd.DataFrame:
    rows = _read_jsonl(run_dir / "loop_metrics.jsonl")
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    for col in ["loop_elapsed_s", "tick_s", "account_count", "price_rows", "synthetic_price_rows"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _save(fig: plt.Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)


def _style_time_axis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(True, alpha=0.22)
    ax.set_xlabel("UTC time")


def plot_feed(df: pd.DataFrame, loop_df: pd.DataFrame, out_dir: Path) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    axes[0].plot(df["ts"], df["gap_s"], color="#2563eb", lw=1.8)
    axes[0].axhline(90, color="#ef4444", ls="--", lw=1.2, alpha=0.8)
    axes[0].set_ylabel("Tick gap (s)")
    axes[0].set_title("Feed Archive Tick Gaps")
    axes[0].grid(True, alpha=0.22)

    axes[1].plot(df["ts"], df["changed_price_rows"], color="#16a34a", lw=1.8)
    axes[1].set_ylabel("Changed rows")
    axes[1].set_title("Price Rows Changed Since Previous Archive Tick")
    axes[1].grid(True, alpha=0.22)

    fallback_col = (
        "synthetic_price_rows"
        if "synthetic_price_rows" in df.columns and df["synthetic_price_rows"].notna().any()
        else "inferred_missing_bbo_rows"
    )
    axes[2].plot(df["ts"], df[fallback_col], color="#f59e0b", lw=1.8)
    axes[2].set_ylabel("Rows")
    axes[2].set_title("Synthetic/Missing-BBO Rows")
    _style_time_axis(axes[2])
    _save(fig, out_dir / "feed_tick_gap_and_price_changes.png")

    if not loop_df.empty:
        fig, ax = plt.subplots(figsize=(14, 6))
        ax.plot(loop_df["ts"], loop_df["loop_elapsed_s"], color="#7c3aed", lw=1.8, label="loop elapsed")
        ax.plot(loop_df["ts"], loop_df["tick_s"], color="#111827", lw=1.2, ls="--", label="target tick")
        ax.set_title("Arena Loop Elapsed Time")
        ax.set_ylabel("Seconds")
        _style_time_axis(ax)
        ax.legend()
        _save(fig, out_dir / "arena_loop_elapsed.png")


def _summarize_numeric(vals: list[float]) -> dict[str, float | None]:
    vals = [float(v) for v in vals if v is not None and pd.notna(v)]
    if not vals:
        return {"min": None, "median": None, "p95": None, "max": None}
    return {
        "min": min(vals),
        "median": statistics.median(vals),
        "p95": sorted(vals)[min(len(vals) - 1, max(0, math.ceil(0.95 * len(vals)) - 1))],
        "max": max(vals),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Report Polymarket feed/archive latency for a live arena run")
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--base-dir", default="data/reports/live_competitor_tests")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else _latest_run(Path(args.base_dir))
    out_dir = Path(args.out_dir) if args.out_dir else run_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = _read_gzip_jsonl(run_dir / "real_input_ticks.jsonl.gz")
    df = build_feed_frame(rows)
    loop_df = build_loop_frame(run_dir)
    df.to_csv(out_dir / "feed_latency_summary.csv", index=False)
    if not loop_df.empty:
        loop_df.to_csv(out_dir / "loop_metrics_summary.csv", index=False)
    plot_feed(df, loop_df, out_dir)

    gap_summary = _summarize_numeric(df["gap_s"].dropna().tolist())
    changed_summary = _summarize_numeric(df["changed_price_rows"].dropna().tolist())
    missing_summary = _summarize_numeric(df["inferred_missing_bbo_rows"].dropna().tolist())
    print(f"run_dir={run_dir}")
    print(f"out_dir={out_dir}")
    print(f"ticks={len(df)}")
    print(
        "tick_gap_s "
        f"median={gap_summary['median']} p95={gap_summary['p95']} max={gap_summary['max']} "
        f"gaps_over_90s={int((df['gap_s'] > 90).sum())}"
    )
    print(
        "changed_price_rows "
        f"median={changed_summary['median']} p95={changed_summary['p95']} max={changed_summary['max']}"
    )
    print(
        "synthetic_or_missing_bbo_rows "
        f"median={missing_summary['median']} p95={missing_summary['p95']} max={missing_summary['max']}"
    )
    if not loop_df.empty:
        loop_summary = _summarize_numeric(loop_df["loop_elapsed_s"].dropna().tolist())
        print(
            "loop_elapsed_s "
            f"median={loop_summary['median']} p95={loop_summary['p95']} max={loop_summary['max']}"
        )
    else:
        print("loop_elapsed_s unavailable: run predates loop_metrics.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
