"""Visualize the actual bets in a competitor run.

Every completed bet is reconstructed from the SELL trade records (which carry
entry_price, exit_price, hold_seconds and pnl). Bets are grouped into the
(event, bracket, side) VARIATIONS the bot traded, and each variation is drawn
on a time x price plane:

  - faint segment per individual trade (entry -> exit)  -> shows the spread
  - one bold arrow per variation: median-entry -> median-exit, mean prices
  - colour : green if the variation's total P&L > 0, red if < 0
  - arrow width : proportional to total $ deployed in that variation
  - label : "<bracket> <side>  n=<trades>  $<total pnl>"

Faceted by event, one panel each. One figure per run.

Usage:
  python -m research.plot_bets --run-dir <path> --out notebooks/png/bets.png
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from research.run_select import latest_run

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

GREEN, RED = "#16a34a", "#dc2626"


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def load_completed_bets(run_dir: Path) -> list[dict]:
    """One dict per SELL = one completed round-trip bet."""
    bets: list[dict] = []
    for fp in glob.glob(str(run_dir / "competitors" / "*" / "trades.jsonl")):
        try:
            with open(fp, "rb") as f:
                for raw in f:
                    if not raw.strip():
                        continue
                    try:
                        t = json.loads(raw)
                    except Exception:
                        continue
                    if t.get("action") != "SELL":
                        continue
                    exit_ts = _parse(t.get("ts"))
                    if exit_ts is None:
                        continue
                    entry_ts = exit_ts - timedelta(seconds=float(t.get("hold_seconds") or 0))
                    ep = float(t.get("entry_price") or 0.0)
                    shares = float(t.get("shares") or 0.0)
                    cost = float(t.get("cost") or 0.0) or shares * ep
                    bets.append({
                        "event": t.get("event_slug") or "?",
                        "label": t.get("label") or "?",
                        "side": t.get("side") or "?",
                        "entry_ts": entry_ts,
                        "exit_ts": exit_ts,
                        "entry_price": ep,
                        "exit_price": float(t.get("exit_price") or 0.0),
                        "cost": cost,
                        "pnl": float(t.get("pnl") or 0.0),
                    })
        except Exception:
            pass
    return bets


def _median(xs: list) -> object:
    return sorted(xs)[len(xs) // 2]


def plot_run(bets: list[dict], out_path: Path, title: str) -> None:
    by_event: dict[str, list[dict]] = collections.defaultdict(list)
    for b in bets:
        by_event[b["event"]].append(b)
    events = sorted(by_event)
    if not events:
        # No SELL events yet — early in a hold-to-resolution run, or LIVE
        # before the first bracket has resolved. Overwrite with a placeholder
        # so the user isn't left staring at a stale bets.png from a previous
        # run (which is what happens if we silently skip).
        fig, ax = plt.subplots(figsize=(12, 4))
        ax.text(0.5, 0.6, "No completed bets yet",
                ha="center", va="center", fontsize=18, color="#374151")
        ax.text(0.5, 0.4,
                "Hold-to-resolution: positions stay open until their bracket "
                "ends.\nbets.png will populate after the first resolution.",
                ha="center", va="center", fontsize=10, color="#6b7280")
        ax.set_axis_off()
        fig.suptitle(title, fontsize=12)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=160)
        plt.close(fig)
        print(f"wrote {out_path}  (placeholder - 0 completed bets yet)")
        return

    fig, axes = plt.subplots(len(events), 1, figsize=(15, 4.4 * len(events)),
                             squeeze=False)
    for ax, ev in zip(axes[:, 0], events):
        evbets = by_event[ev]
        # faint per-trade segments — the cloud shows the real spread
        for b in evbets:
            ax.plot([b["entry_ts"], b["exit_ts"]], [b["entry_price"], b["exit_price"]],
                    color=(GREEN if b["pnl"] >= 0 else RED), alpha=0.05, lw=0.9, zorder=1)

        groups: dict[tuple, list[dict]] = collections.defaultdict(list)
        for b in evbets:
            groups[(b["label"], b["side"])].append(b)
        max_cost = max((sum(x["cost"] for x in g) for g in groups.values()), default=1.0)

        # draw bigger-money variations last so they sit on top
        for (label, side), g in sorted(groups.items(),
                                       key=lambda kv: sum(x["cost"] for x in kv[1])):
            tot_cost = sum(x["cost"] for x in g)
            tot_pnl = sum(x["pnl"] for x in g)
            e_ts = _median([x["entry_ts"] for x in g])
            x_ts = _median([x["exit_ts"] for x in g])
            e_p = statistics.mean(x["entry_price"] for x in g)
            x_p = statistics.mean(x["exit_price"] for x in g)
            c = GREEN if tot_pnl >= 0 else RED
            lw = 1.5 + 7.0 * (tot_cost / max_cost)
            ax.annotate("", xy=(x_ts, x_p), xytext=(e_ts, e_p),
                        arrowprops=dict(arrowstyle="-|>", color=c, lw=lw, alpha=0.9),
                        zorder=3)
            ax.plot([e_ts], [e_p], "o", color=c, ms=4, zorder=4)
            ax.text(x_ts, x_p, f"  {label} {side}  n={len(g)} ${tot_pnl:+,.0f}",
                    fontsize=6.5, color=c, va="center", zorder=5)

        ev_pnl = sum(b["pnl"] for b in evbets)
        ev_cost = sum(b["cost"] for b in evbets)
        ax.set_title(f"{ev}   —   {len(evbets)} bets · ${ev_cost:,.0f} deployed · "
                     f"P&L ${ev_pnl:+,.0f}", fontsize=10)
        ax.set_ylabel("price (entry → exit)")
        ax.set_ylim(-0.04, 1.04)
        ax.grid(True, alpha=0.2)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(mdates.AutoDateLocator()))

    fig.suptitle(title, fontsize=13, y=0.999)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    print(f"wrote {out_path}  ({len(bets)} completed bets, {len(events)} events)")


def main() -> int:
    # Arg shape matches the other plot scripts so refresh_plots can drive it
    # uniformly: --run-dir + --out-dir. Writes <out-dir>/bets.png.
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--base-dir", default="data/reports/live_competitor_tests")
    ap.add_argument("--out-dir", default="notebooks/png")
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else latest_run(Path(args.base_dir), "v4")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    bets = load_completed_bets(run_dir)
    plot_run(bets, out_dir / "bets.png", args.title or f"Bets — {run_dir.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
