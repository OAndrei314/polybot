"""
Account-pool lane monitor — the instrument for scaling to fast detection.

The detection edge is gated by the *sustainable* polling cadence across the
account pool: lane_s ~= 18s / N_healthy_accounts (the ~50-req/15min per-cookie
budget), and detection latency ~= lane_s / 2. SSE push is not durable (X idles
it out), so this is the real fast path.

This tool runs the live AccountRotator and reports, every few seconds:
  - measured sustainable lane interval (the rotator's own aggregate_interval_s)
  - implied detection latency vs the ~6s book reprice we need to beat
  - per-account health: ok / 429 / budget-paced / dead, and rate-limit budget

As you append accounts to accounts.json, run this and watch lane_s fall toward
sub-second. Dying cookies show up immediately so you can replace them — that's
how you keep N_healthy high without over-buying accounts.

    python -m research.lane_monitor --watch-s 5
"""

from __future__ import annotations

import argparse
import os
import threading
import time

os.environ.setdefault("POLYBOT_ENABLE_SSE_POOL", "0")  # polling is the durable path

from signals.account_pool import load_accounts, AccountRotator
from signals.stream import TweetStream

BOOK_REPRICE_S = 6.0   # measured median market reprice after a tweet


def _classify(lane_s: float) -> str:
    detect = lane_s / 2.0
    if detect < 1.3:
        return f"SUB-1.3s — front of the fast pack (detect~{detect:.2f}s)"
    if detect < BOOK_REPRICE_S:
        return f"beats 6s reprice but slower than fast pack (detect~{detect:.2f}s)"
    return f"TOO SLOW — loses to reprice (detect~{detect:.2f}s)"


def run(watch_s: float, duration_s: float) -> int:
    accounts = load_accounts()
    n = len(accounts)
    print(f"loaded {n} accounts from accounts.json / env")
    if n == 0:
        print("no accounts — nothing to monitor")
        return 1
    print(f"target for sub-second sustainable detection: ~{max(1, round(18*2/1.0))}+ healthy accounts "
          f"(lane_s=18/N, detect=lane_s/2)\n")

    # Drive a real rotator the same way TweetStream does, so the cadence is real.
    stream = TweetStream()
    stream.start()
    t_end = time.time() + duration_s
    try:
        while time.time() < t_end:
            st = stream.account_status()
            rot = st.get("gql_rotator", {}) or {}
            sse = st.get("sse", {}) or {}
            lane = rot.get("aggregate_interval_s")
            total = rot.get("total", n)
            active = rot.get("active", 0)
            cooling = rot.get("cooling", 0)
            dead = rot.get("dead", 0)
            healthy = total - dead
            print("\033c", end="")
            print(time.strftime("lane_monitor %Y-%m-%d %H:%M:%S"))
            print(f"accounts: total={total} healthy={healthy} active_now={active} cooling={cooling} dead={dead}")
            if lane is not None:
                print(f"sustainable lane_s = {lane:.2f}s  ->  {_classify(float(lane))}")
            # project what more accounts would buy
            if healthy > 0 and lane:
                per = float(lane) * healthy   # ~ per-account cycle gap
                for tgt in (25, 50, 100):
                    if tgt > healthy:
                        proj = per / tgt
                        print(f"   projected at {tgt} healthy: lane_s~={proj:.2f}s detect~={proj/2:.2f}s")
            print("\nper-account budget/status:")
            budget = rot.get("budget", {}) or {}
            for label, b in list(budget.items())[:40]:
                flag = "DEAD" if b.get("dead") else (b.get("status") or "?")
                rem = b.get("remaining")
                rin = b.get("resets_in_s")
                nxt = b.get("account_next_poll_in_s")
                print(f"  {label:10} {str(flag)[:34]:34} rem={rem} reset_in={rin} next_poll_in={nxt}")
            time.sleep(watch_s)
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="account-pool lane monitor")
    p.add_argument("--watch-s", type=float, default=5.0)
    p.add_argument("--duration", type=float, default=3600.0)
    a = p.parse_args(argv)
    return run(a.watch_s, a.duration)


if __name__ == "__main__":
    raise SystemExit(main())
