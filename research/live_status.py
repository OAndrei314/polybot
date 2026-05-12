from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def latest_run(base_dir: str) -> Path | None:
    root = Path(base_dir)
    if not root.exists():
        return None
    runs = sorted(root.glob("*_real_48h"), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_live_pid() -> int | None:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "research.live_competitor_test"],
            text=True,
        )
    except Exception:
        return None
    pids = []
    for line in out.splitlines():
        try:
            pid = int(line.strip())
        except Exception:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids[0] if pids else None


def find_caffeinate_pid() -> int | None:
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", "caffeinate"],
            text=True,
        )
    except Exception:
        return None
    matches: list[tuple[int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        if pid != os.getpid() and "caffeinate" in cmd:
            matches.append((pid, cmd))
    if not matches:
        return None
    # Prefer the long-lived guard used for live arena runs when present.
    for pid, cmd in reversed(matches):
        if "-dims" in cmd:
            return pid
    return matches[-1][0]


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def last_input_tick(path: Path) -> tuple[int, dict[str, Any] | None, float | None, int]:
    if not path.exists():
        return 0, None, None, 0
    rows = []
    with gzip.open(path, "rt") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    gaps_over_90 = 0
    prev_ts = None
    max_gap = None
    for row in rows:
        try:
            ts = row["ts"].replace("Z", "+00:00")
            import datetime as _dt

            cur = _dt.datetime.fromisoformat(ts).timestamp()
        except Exception:
            continue
        if prev_ts is not None:
            gap = cur - prev_ts
            max_gap = gap if max_gap is None else max(max_gap, gap)
            if gap > 90:
                gaps_over_90 += 1
        prev_ts = cur
    return len(rows), rows[-1] if rows else None, max_gap, gaps_over_90


def read_leaderboard(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for row in csv.DictReader(f):
            for key in ["equity", "total_pnl", "open_positions", "sells", "stop_loss_churn"]:
                try:
                    row[key] = float(row[key])
                except Exception:
                    row[key] = 0.0
            rows.append(row)
    return sorted(rows, key=lambda r: float(r.get("total_pnl") or 0.0), reverse=True)


def print_status(args: argparse.Namespace) -> int:
    run = latest_run(args.base_dir)
    live_pid = find_live_pid()
    caffeinate_pid = read_pid(Path(args.pid_file))
    if not alive(caffeinate_pid):
        caffeinate_pid = read_pid(Path(args.base_dir) / "caffeinate_v2_48h.pid")
    if not alive(caffeinate_pid):
        caffeinate_pid = read_pid(Path(args.base_dir) / "caffeinate_17511.pid")
    if not alive(caffeinate_pid):
        caffeinate_pid = find_caffeinate_pid()
    print(f"arena_pid={live_pid or 'n/a'} alive={alive(live_pid)}")
    print(f"caffeinate_pid={caffeinate_pid or 'n/a'} alive={alive(caffeinate_pid)}")
    if run is None:
        print("run_dir=n/a")
        return 1
    print(f"run_dir={run}")

    input_path = run / "real_input_ticks.jsonl.gz"
    tick_count, tick, max_gap, gaps_over_90 = last_input_tick(input_path)
    age = time.time() - input_path.stat().st_mtime if input_path.exists() else None
    print(f"input_ticks={tick_count} input_file_age_s={age:.1f}" if age is not None else "input_ticks=0")
    if tick:
        feed_state = tick.get("price_feed_state", {}) or {}
        print(
            "latest_feed "
            f"ts={tick.get('ts')} "
            f"prices={len(tick.get('price_rows') or [])} "
            f"synthetic={tick.get('synthetic_price_rows', 'n/a')} "
            f"count_source={tick.get('tweet_state', {}).get('count_source')} "
            f"sse={tick.get('health_state', {}).get('sse_alive')} "
            f"xtracker={tick.get('health_state', {}).get('xtracker_alive')} "
            f"clob_ws={feed_state.get('ws_ok', 'n/a')} "
            f"last_ws_age_s={feed_state.get('last_ws_event_age_s', 'n/a')}"
        )
    if max_gap is not None:
        print(f"stream_gaps max_gap_s={max_gap:.1f} gaps_over_90s={gaps_over_90}")

    rows = read_leaderboard(run / "leaderboard_latest.csv")
    if rows:
        total_open = sum(int(float(row.get("open_positions") or 0.0)) for row in rows)
        active = sum(
            1
            for row in rows
            if int(float(row.get("open_positions") or 0.0))
            or int(float(row.get("buys") or 0.0))
            or int(float(row.get("sells") or 0.0))
        )
        total_buys = sum(int(float(row.get("buys") or 0.0)) for row in rows)
        total_sells = sum(int(float(row.get("sells") or 0.0)) for row in rows)
        print(
            "arena_totals "
            f"competitors={len(rows)} active={active} "
            f"open_positions={total_open} buys={total_buys} sells={total_sells}"
        )
        print("top_pnl")
        for row in rows[: args.top]:
            print(
                f"  {row['competitor_id']:>8} "
                f"pnl={float(row['total_pnl']):+8.2f} "
                f"equity={float(row['equity']):8.2f} "
                f"open={int(float(row['open_positions']))} "
                f"sells={int(float(row['sells']))}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact live arena status")
    parser.add_argument("--base-dir", default="data/reports/live_competitor_tests")
    parser.add_argument("--pid-file", default="data/reports/live_competitor_tests/caffeinate_48h.pid")
    parser.add_argument("--top", type=int, default=8, help="Number of leaderboard rows to show")
    parser.add_argument("--watch-s", type=float, default=0.0, help="Refresh interval; 0 means print once")
    args = parser.parse_args()

    if args.watch_s <= 0:
        return print_status(args)

    while True:
        print("\033c", end="")
        print(time.strftime("live_status %Y-%m-%d %H:%M:%S %Z"))
        code = print_status(args)
        try:
            time.sleep(args.watch_s)
        except KeyboardInterrupt:
            return code


if __name__ == "__main__":
    raise SystemExit(main())
