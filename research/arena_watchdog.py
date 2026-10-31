"""Polybot arena watchdog.

Monitors the arena + snapshot watcher and:
  - Sends a Windows toast notification on any health issue
  - Auto-restarts the arena if it dies
  - Auto-restarts the snapshot watcher if it dies
  - Detects "ticks stalled" (process alive but not writing data) -- restarts
  - Logs every event to logs/watchdog.log

Health checks every CHECK_INTERVAL_S seconds.

Failure modes detected:
  1. Arena process gone (PID died)
  2. Snapshot watcher gone
  3. Arena stale (no new tick written in STALL_THRESHOLD_S seconds)
  4. All 5 GQL accounts dead (no signal possible)
  5. Disk free < 5 GB (running out of space)

Auto-restart logic:
  - Counts consecutive restarts in a rolling 10-minute window
  - If >5 restarts in 10 min -> stop trying, only alert (something fundamental is broken)

Usage from repo root:
    python -m research.arena_watchdog --duration-h 168
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from research import jsonl_io
from research.run_select import latest_run as select_latest_run

try:
    from winotify import Notification, audio
    HAS_WINOTIFY = True
except Exception:
    HAS_WINOTIFY = False

ROOT = Path(".")
LOG_PATH = ROOT / "logs" / "watchdog.log"
LIVE_TESTS_DIR = ROOT / "data" / "reports" / "live_competitor_tests"

CHECK_INTERVAL_S = 60.0
STALL_THRESHOLD_S = 180.0        # 3 min of no tick = stalled
STARTUP_GRACE_S = 300.0          # don't kill arena for stall in the first 5 min
                                 # after watchdog start - loop_metrics.jsonl mtime
                                 # lags the actual first tick, and a "stalled"
                                 # reading immediately on watchdog launch is
                                 # almost always a fresh-arena false positive
                                 # (see bug 3 from 2026-05-26 v4 restart loss)
RESTART_WINDOW_S = 600.0         # 10 min rolling window
MAX_RESTARTS_IN_WINDOW = 5
MIN_DISK_FREE_GB = 5.0
APP_ID = "Polybot Arena"

# Track restart history
restart_history: collections.deque[float] = collections.deque(maxlen=20)


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def toast(title: str, body: str, urgent: bool = False) -> None:
    """Show a Windows toast notification."""
    if not HAS_WINOTIFY:
        log(f"WOULD TOAST: [{title}] {body}")
        return
    try:
        n = Notification(app_id=APP_ID, title=title, msg=body[:200])
        if urgent:
            n.set_audio(audio.LoopingAlarm2, loop=False)
        n.show()
    except Exception as e:
        log(f"toast failed: {e}")


def get_python_procs() -> list[dict]:
    """Returns list of {pid, cmdline} for all running python.exe processes."""
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout) if out.stdout.strip() else []
        if isinstance(data, dict):
            data = [data]
        return [{"pid": d.get("ProcessId"), "cmdline": d.get("CommandLine") or ""} for d in data]
    except Exception as e:
        log(f"get_python_procs failed: {e}")
        return []


def find_proc(pattern: str) -> dict | None:
    for p in get_python_procs():
        if pattern in p["cmdline"]:
            return p
    return None


def latest_run() -> Path | None:
    try:
        return select_latest_run(LIVE_TESTS_DIR, "v4")
    except FileNotFoundError:
        return None


def tick_age_seconds() -> float | None:
    """Returns seconds since the latest tick was written to loop_metrics.jsonl."""
    rd = latest_run()
    if not rd:
        return None
    lm = rd / "loop_metrics.jsonl"
    ts = jsonl_io.mtime(lm)
    if ts is None:
        return None
    return time.time() - ts


def gql_account_health() -> tuple[int, int] | None:
    """Returns (alive_count, total_count) from latest loop_metrics."""
    rd = latest_run()
    if not rd:
        return None
    lm = rd / "loop_metrics.jsonl"
    if not jsonl_io.exists(lm):
        return None
    try:
        tail = jsonl_io.last_line(lm)
        if not tail:
            return None
        d = json.loads(tail)
        sb = d.get("signal_budget") or {}
        total = int(sb.get("gql_total") or 0)
        dead = int(sb.get("gql_dead") or 0)
        return total - dead, total
    except Exception:
        return None


def disk_free_gb() -> float:
    free = shutil.disk_usage(ROOT).free
    return free / (1024 ** 3)


def can_restart() -> bool:
    """Check if we're within the restart budget."""
    now = time.time()
    # Drop entries outside the window
    while restart_history and now - restart_history[0] > RESTART_WINDOW_S:
        restart_history.popleft()
    return len(restart_history) < MAX_RESTARTS_IN_WINDOW


def start_arena(duration_h: float, max_competitors: int) -> int | None:
    env = os.environ.copy()
    env.update({
        "POLYBOT_FAST_GQL": "1",
        "POLYBOT_ENABLE_SSE_POOL": "0",
        "POLYBOT_GQL_MIN_INTERVAL_S": "6.5",
        "POLYBOT_GQL_JITTER_S": "1.5",
        "POLYBOT_GQL_RATE_SLACK_S": "0.5",
        "POLYBOT_XTRACKER_TTL_S": "20",
    })
    out = ROOT / "logs" / "arena_v4_stdout.log"
    err = ROOT / "logs" / "arena_v4_stderr.log"
    cmd = [
        sys.executable, "-u", "-m", "research.live_competitor_test",
        "--duration-h", str(int(duration_h)),
        "--tick-s", "1",
        "--report-s", "60",
        "--input-archive-s", "5",
        "--health-poll-s", "30",
        "--max-competitors", str(int(max_competitors)),
        "--configs-glob", "data/experiments/96h_pool/*.json",
        "--out-dir", "data/reports/live_competitor_tests",
    ]
    with out.open("ab") as ofh, err.open("ab") as efh:
        # CREATE_NEW_PROCESS_GROUP detaches from us so we don't kill it on watchdog exit
        proc = subprocess.Popen(
            cmd, env=env, cwd=str(ROOT), stdout=ofh, stderr=efh,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        return proc.pid


def start_snapshot(duration_h: float) -> int | None:
    out = ROOT / "logs" / "snapshot_v4_stdout.log"
    err = ROOT / "logs" / "snapshot_v4_stderr.log"
    cmd = [
        sys.executable, "-u", "-m", "research.snapshot_96h",
        "--interval-min", "120",
        "--duration-h", str(int(duration_h)),
    ]
    with out.open("ab") as ofh, err.open("ab") as efh:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=ofh, stderr=efh,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        return proc.pid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-h", type=float, default=168.0,
                    help="watchdog runs for this long, restarts use remaining time")
    ap.add_argument("--max-competitors", type=int, default=200)
    ap.add_argument("--once", action="store_true", help="run one check then exit (for testing)")
    args = ap.parse_args()

    deadline = time.time() + args.duration_h * 3600.0
    startup_time = time.time()
    log(f"watchdog starting. duration={args.duration_h}h. deadline UTC={datetime.fromtimestamp(deadline, timezone.utc).isoformat()}")
    log(f"startup grace: {STARTUP_GRACE_S:.0f}s (no stall-kill in first {STARTUP_GRACE_S/60:.0f} min)")
    log(f"toast notifications: {'ON' if HAS_WINOTIFY else 'NOT AVAILABLE'}")
    toast("Watchdog started", f"Monitoring arena for {args.duration_h:.0f} hours")

    check_n = 0
    last_known_arena_pid = None
    last_known_snap_pid = None

    while time.time() < deadline:
        check_n += 1
        now = time.time()
        time_left_h = (deadline - now) / 3600.0
        remaining_h = max(1.0, time_left_h)

        try:
            # === Check 1: disk space ===
            free_gb = disk_free_gb()
            if free_gb < MIN_DISK_FREE_GB:
                msg = f"Disk free is {free_gb:.1f} GB (< {MIN_DISK_FREE_GB} GB threshold)"
                log(f"ALERT: {msg}")
                toast("Disk space LOW", msg, urgent=True)

            # === Check 2: arena process ===
            arena = find_proc("data/experiments/96h_pool")
            if arena is None:
                msg = f"Arena process is DEAD (was PID {last_known_arena_pid})"
                log(f"ALERT: {msg}")
                if can_restart():
                    new_pid = start_arena(remaining_h, args.max_competitors)
                    restart_history.append(now)
                    log(f"  -> restarted arena, new PID {new_pid}, remaining={remaining_h:.1f}h")
                    toast("Arena restarted", f"Crash detected. Restarted with PID {new_pid}", urgent=True)
                    last_known_arena_pid = new_pid
                    time.sleep(20)  # give it time to spin up
                else:
                    msg2 = f"Arena crashed {MAX_RESTARTS_IN_WINDOW}+ times in 10 min. NOT restarting."
                    log(f"ALERT: {msg2}")
                    toast("ARENA REPEATEDLY FAILING", msg2, urgent=True)
            else:
                if last_known_arena_pid != arena["pid"]:
                    log(f"arena PID = {arena['pid']}")
                    last_known_arena_pid = arena["pid"]

            # === Check 3: snapshot watcher ===
            snap = find_proc("snapshot_96h")
            if snap is None:
                msg = f"Snapshot watcher DEAD (was PID {last_known_snap_pid})"
                log(f"ALERT: {msg}")
                new_pid = start_snapshot(remaining_h)
                log(f"  -> restarted snapshot, new PID {new_pid}")
                toast("Snapshot watcher restarted", f"New PID {new_pid}")
                last_known_snap_pid = new_pid
            else:
                if last_known_snap_pid != snap["pid"]:
                    log(f"snapshot PID = {snap['pid']}")
                    last_known_snap_pid = snap["pid"]

            # === Check 4: tick freshness ===
            age = tick_age_seconds()
            if age is None:
                # OK if arena just started; skip
                pass
            elif age > STALL_THRESHOLD_S:
                msg = f"Arena STALLED. Last tick written {age:.0f}s ago (threshold {STALL_THRESHOLD_S}s)"
                log(f"ALERT: {msg}")
                if (now - startup_time) < STARTUP_GRACE_S:
                    log(f"  -> within {STARTUP_GRACE_S:.0f}s startup grace; NOT killing. Waiting for arena to write fresh loop_metrics.")
                elif arena and can_restart():
                    log(f"  -> killing stalled arena PID {arena['pid']} and restarting")
                    try:
                        subprocess.run(
                            ["powershell.exe", "-NoProfile", "-Command",
                             f"Stop-Process -Id {arena['pid']} -Force"],
                            capture_output=True, timeout=10,
                        )
                    except Exception:
                        pass
                    time.sleep(3)
                    new_pid = start_arena(remaining_h, args.max_competitors)
                    restart_history.append(now)
                    toast("Arena was stalled -- restarted", f"Was silent for {age:.0f}s, restarted as PID {new_pid}", urgent=True)
                    last_known_arena_pid = new_pid
                    time.sleep(20)

            # === Check 5: GQL account health ===
            gql = gql_account_health()
            if gql is not None:
                alive, total = gql
                if total > 0 and alive == 0:
                    msg = f"All {total} GQL accounts are DEAD. Bot has no signal."
                    log(f"ALERT: {msg}")
                    toast("All GQL accounts DEAD", msg, urgent=True)
                elif total > 0 and alive < total / 2:
                    msg = f"Only {alive}/{total} GQL accounts alive"
                    log(f"WARN: {msg}")
                    toast("GQL accounts degraded", msg)

            if check_n % 30 == 0:
                # Periodic OK heartbeat in the log
                age_s = f"{age:.0f}s" if age is not None else "unknown"
                log(f"OK check #{check_n}. arena={arena['pid'] if arena else 'DEAD'} snap={snap['pid'] if snap else 'DEAD'} tick_age={age_s} disk={free_gb:.1f}GB")

        except Exception as e:
            import traceback
            log(f"watchdog ERROR: {traceback.format_exc()}")
            toast("Watchdog ERROR", str(e)[:100])

        if args.once:
            return 0

        time.sleep(CHECK_INTERVAL_S)

    log(f"watchdog deadline reached after {check_n} checks. Exiting.")
    toast("Watchdog done", f"168h experiment window ended after {check_n} health checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
