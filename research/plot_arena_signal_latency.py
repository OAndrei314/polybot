"""Plot tweet-detection latency and per-account budget over time.

Reads `loop_metrics.jsonl` from the latest (or specified) live_competitor_test
run and emits:

  - arena_signal_latency.png   detection latency (lane/2) + worst-case (lane) over time
  - arena_account_pace.png     per-account pace (s) over time
  - arena_account_remaining.png per-account x-rate-limit-remaining over time
  - arena_account_state.png    per-account ok / rate-limited / dead state over time

Usage:
  python -m research.plot_arena_signal_latency
  python -m research.plot_arena_signal_latency --run-dir <path> --out-dir notebooks/png
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from research import jsonl_io
from research.run_select import latest_run

try:
    import orjson as _fastjson
    _LOADS = _fastjson.loads
except ImportError:
    _LOADS = json.loads

_STATUS_RE = re.compile(r"rem=(\d+)[^0-9]+pace=([0-9.]+)s")

ACCOUNT_COLORS = {
    "qojjox": "#dc2626",
    "latenightsatire": "#2563eb",
    "acct3": "#16a34a",
    "acct4": "#a16207",
}


def _color(label: str, fallback_idx: int) -> str:
    if label in ACCOUNT_COLORS:
        return ACCOUNT_COLORS[label]
    palette = ["#dc2626", "#2563eb", "#16a34a", "#a16207", "#7c3aed",
               "#db2777", "#0891b2", "#65a30d", "#ea580c", "#0f766e"]
    return palette[fallback_idx % len(palette)]


def latest_run_dir(base: Path) -> Path:
    try:
        return latest_run(base, "v4")
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc


def parse_status(s: str) -> tuple[int | None, float | None, str]:
    """Returns (remaining, pace_s, state) where state in {ok, rate_limited, dead, unknown}."""
    if not isinstance(s, str) or not s:
        return None, None, "unknown"
    low = s.lower()
    if low.startswith("dead"):
        return None, None, "dead"
    if "rate_limited" in low or "429" in low or "budget_empty" in low:
        return None, None, "rate_limited"
    m = _STATUS_RE.search(s)
    if m:
        try:
            return int(m.group(1)), float(m.group(2)), "ok"
        except Exception:
            pass
    if low.startswith("ok"):
        return None, None, "ok"
    return None, None, "unknown"


def load_metrics(run_dir: Path) -> list[dict]:
    path = run_dir / "loop_metrics.jsonl"
    if not jsonl_io.exists(path):
        raise SystemExit(f"missing {path}")
    rows: list[dict] = []
    with jsonl_io.open_binary(path) as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                rows.append(_LOADS(raw))
            except Exception:
                continue
    return rows


def plot_signal_latency(run_dir: Path, out_dir: Path) -> None:
    rows = load_metrics(run_dir)
    if not rows:
        print("no rows")
        return

    times: list[datetime] = []
    avg_lat: list[float] = []
    worst_lat: list[float] = []
    for r in rows:
        sb = r.get("signal_budget") or {}
        lane = sb.get("gql_lane_s")
        if lane is None:
            continue
        try:
            lane = float(lane)
        except Exception:
            continue
        try:
            times.append(datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")))
        except Exception:
            continue
        avg_lat.append(lane / 2.0)
        worst_lat.append(lane)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(times, worst_lat, color="#dc2626", lw=1.2, alpha=0.7, label="worst case (lane spacing)")
    ax.plot(times, avg_lat, color="#1d4ed8", lw=1.6, label="average detection latency (lane / 2)")
    ax.axhline(0.2, color="#16a34a", ls="--", lw=1, label="200 ms target")
    ax.set_ylabel("seconds")
    ax.set_xlabel("time (UTC)")
    ax.set_title(f"Tweet-detection latency over time\n{run_dir.name}")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    out = out_dir / "arena_signal_latency.png"
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"wrote {out}")


def plot_account_series(run_dir: Path, out_dir: Path) -> None:
    rows = load_metrics(run_dir)
    if not rows:
        return

    series: dict[str, dict[str, list]] = {}
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
        except Exception:
            continue
        statuses = ((r.get("signal_budget") or {}).get("gql_statuses") or {})
        rate_limited = set((r.get("signal_budget") or {}).get("gql_rate_limited_accounts") or [])
        low_budget = set((r.get("signal_budget") or {}).get("gql_low_budget_accounts") or [])
        for label, s in statuses.items():
            rem, pace, state = parse_status(str(s))
            if label in rate_limited:
                state = "rate_limited"
            elif label in low_budget and state == "ok":
                state = "low_budget"
            bucket = series.setdefault(label, {"t": [], "rem": [], "pace": [], "state": []})
            bucket["t"].append(ts)
            bucket["rem"].append(rem if rem is not None else float("nan"))
            bucket["pace"].append(pace if pace is not None else float("nan"))
            bucket["state"].append(state)

    if not series:
        return

    labels = sorted(series.keys())

    # pace plot
    fig, ax = plt.subplots(figsize=(12, 5))
    for i, lbl in enumerate(labels):
        s = series[lbl]
        ax.plot(s["t"], s["pace"], lw=1.2, color=_color(lbl, i), label=lbl, alpha=0.85)
    ax.set_ylabel("pace (s between picks for this account)")
    ax.set_xlabel("time (UTC)")
    ax.set_title(f"Per-account GQL pace over time\n{run_dir.name}")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    out = out_dir / "arena_account_pace.png"
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"wrote {out}")

    # remaining plot
    fig, ax = plt.subplots(figsize=(12, 5))
    for i, lbl in enumerate(labels):
        s = series[lbl]
        ax.plot(s["t"], s["rem"], lw=1.2, color=_color(lbl, i), label=lbl, alpha=0.85)
    ax.set_ylabel("x-rate-limit-remaining (calls left until reset)")
    ax.set_xlabel("time (UTC)")
    ax.set_title(f"Per-account remaining budget over time\n{run_dir.name}")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    out = out_dir / "arena_account_remaining.png"
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"wrote {out}")

    # state strip plot — one horizontal lane per account.
    # Batch all points of the same color into ONE scatter call (was 54k+
    # individual scatter() calls on a 24h run, which took 80+ minutes).
    state_color = {
        "ok": "#16a34a",
        "low_budget": "#ca8a04",
        "rate_limited": "#dc2626",
        "dead": "#1f2937",
        "unknown": "#9ca3af",
    }
    fig, ax = plt.subplots(figsize=(12, 1.0 + 0.6 * len(labels)))
    by_color: dict[str, tuple[list, list]] = {}
    for row, lbl in enumerate(labels):
        s = series[lbl]
        for t, st in zip(s["t"], s["state"]):
            color = state_color.get(st, "#9ca3af")
            ts_list, y_list = by_color.setdefault(color, ([], []))
            ts_list.append(t)
            y_list.append(row)
    for color, (ts_list, y_list) in by_color.items():
        ax.scatter(ts_list, y_list, color=color, s=8, marker="s")
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.set_xlabel("time (UTC)")
    ax.set_title(f"Per-account state strip (green=ok, yellow=low budget, red=rate-limited, black=dead)\n{run_dir.name}")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(True, axis="x", alpha=0.3)
    fig.autofmt_xdate()
    out = out_dir / "arena_account_state.png"
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"wrote {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=Path("notebooks/png"))
    ap.add_argument(
        "--base",
        type=Path,
        default=Path("data/reports/live_competitor_tests"),
        help="parent dir containing run subdirectories (used if --run-dir not given)",
    )
    args = ap.parse_args()
    run_dir = args.run_dir or latest_run_dir(args.base)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"run_dir={run_dir}")
    print(f"out_dir={args.out_dir}")
    plot_signal_latency(run_dir, args.out_dir)
    plot_account_series(run_dir, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
