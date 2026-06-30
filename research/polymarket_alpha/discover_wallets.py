"""Discover active Polymarket wallets by walking the recent-trades feed.

The data-api `/trades` endpoint returns the most recent N trades globally.
We page through it, collect unique proxyWallet addresses, and persist them.

This is the first step in the smart-money pipeline. After this we can pull
each discovered wallet's full trade history individually via ?user=<addr>.

Output: data/polymarket_alpha/wallets.jsonl.gz
  one line per wallet: {wallet, n_trades_seen, n_markets_seen, min_size, max_size, first_seen, last_seen}

Usage:
  python -m research.polymarket_alpha.discover_wallets --pages 200 --min-size 100
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from collections import defaultdict
from pathlib import Path

from research.polymarket_alpha import api


ROOT = Path("data/polymarket_alpha")
WALLETS_FILE = ROOT / "wallets.jsonl.gz"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=200,
                    help="how many 500-trade pages to walk")
    ap.add_argument("--min-size", type=float, default=100.0,
                    help="ignore trades smaller than this (shares); filters out noise")
    ap.add_argument("--page-size", type=int, default=500)
    ap.add_argument("--merge", action="store_true",
                    help="if wallets.jsonl.gz exists, merge new findings rather than overwrite")
    args = ap.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)

    # Optionally preload prior runs so daily cron accumulates instead of
    # overwriting. Existing wallet entries get their stats merged with new ones.
    existing: dict[str, dict] = {}
    if args.merge and WALLETS_FILE.exists():
        with gzip.open(WALLETS_FILE, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip(): continue
                d = json.loads(line)
                w = d.get("wallet")
                if w:
                    d.setdefault("markets", set())  # re-create as set for accumulation
                    existing[w] = d
        print(f"  loaded {len(existing)} wallets from prior runs (merge mode)")

    wallets: dict[str, dict] = defaultdict(lambda: {
        "wallet": "", "n_trades": 0, "n_markets": 0, "markets": set(),
        "min_size_usd": float("inf"), "max_size_usd": 0.0, "total_size_usd": 0.0,
        "first_seen": None, "last_seen": None,
        "pseudonym": "", "bio": "",
    })

    t_start = time.time()
    total_trades_seen = 0

    for page_n in range(args.pages):
        offset = page_n * args.page_size
        try:
            trades = api.data_trades_recent(limit=args.page_size, offset=offset)
        except api.HardClientError as e:
            print(f"  page {page_n}: API capped pagination at offset {offset} ({e})")
            break
        if not trades:
            print(f"  page {page_n}: empty -- stopping")
            break
        for t in trades:
            sz = float(t.get("size") or 0)
            px = float(t.get("price") or 0)
            usd = sz * px
            if usd < args.min_size:
                continue
            w = t.get("proxyWallet") or ""
            if not w:
                continue
            d = wallets[w]
            if not d["wallet"]:
                d["wallet"] = w
                d["pseudonym"] = t.get("pseudonym") or ""
                d["bio"] = (t.get("bio") or "")[:200]
            d["n_trades"] += 1
            d["markets"].add(t.get("conditionId") or t.get("slug") or "")
            d["min_size_usd"] = min(d["min_size_usd"], usd)
            d["max_size_usd"] = max(d["max_size_usd"], usd)
            d["total_size_usd"] += usd
            ts = int(t.get("timestamp") or 0)
            if ts:
                if d["first_seen"] is None or ts < d["first_seen"]:
                    d["first_seen"] = ts
                if d["last_seen"] is None or ts > d["last_seen"]:
                    d["last_seen"] = ts
        total_trades_seen += len(trades)
        if page_n % 10 == 0 or page_n == args.pages - 1:
            elapsed = time.time() - t_start
            rate = page_n / elapsed if elapsed > 0 else 0
            eta = (args.pages - page_n) / rate if rate > 0 else 0
            print(f"  page {page_n:>4}/{args.pages}  trades_seen={total_trades_seen:>7}  "
                  f"unique_wallets={len(wallets):>5}  rate={rate:.1f}p/s  eta={eta/60:.1f}min")
        if len(trades) < args.page_size:
            print(f"  page {page_n}: short page ({len(trades)}), end of feed reached")
            break

    # Merge with prior runs if requested
    if args.merge:
        for w, e in existing.items():
            if w in wallets:
                # merge new findings into existing entry
                d = wallets[w]
                d["n_trades"] = d["n_trades"] + e.get("n_trades", 0)
                if isinstance(e.get("markets"), set):
                    d["markets"] |= e["markets"]
                d["min_size_usd"] = min(d["min_size_usd"], e.get("min_size_usd", float("inf")))
                d["max_size_usd"] = max(d["max_size_usd"], e.get("max_size_usd", 0))
                d["total_size_usd"] = d["total_size_usd"] + e.get("total_size_usd", 0)
                if e.get("first_seen") and (d["first_seen"] is None or e["first_seen"] < d["first_seen"]):
                    d["first_seen"] = e["first_seen"]
                if e.get("last_seen") and (d["last_seen"] is None or e["last_seen"] > d["last_seen"]):
                    d["last_seen"] = e["last_seen"]
            else:
                # Carry over wallet from previous run that we didn't see this time
                wallets[w] = e

    # Persist
    out = []
    for w, d in wallets.items():
        d_out = dict(d)
        d_out["n_markets"] = len(d["markets"]) if isinstance(d.get("markets"), set) else d.get("n_markets", 0)
        d_out["markets"] = None
        del d_out["markets"]
        out.append(d_out)
    out.sort(key=lambda x: -x["total_size_usd"])

    with gzip.open(WALLETS_FILE, "wt", encoding="utf-8") as f:
        for d in out:
            f.write(json.dumps(d) + "\n")
    print()
    print(f"wrote {len(out)} wallets to {WALLETS_FILE}")
    print(f"  total trades scanned: {total_trades_seen:,}")
    print(f"  wallets with >=10 trades: {sum(1 for d in out if d['n_trades']>=10)}")
    print(f"  wallets with >=100 trades: {sum(1 for d in out if d['n_trades']>=100)}")
    print()
    print(f"  top 10 by total USD volume seen:")
    print(f"  {'wallet':<14s}  {'trades':>6s}  {'markets':>7s}  {'total_usd':>14s}  {'max_trade':>10s}  pseudonym")
    for d in out[:10]:
        pn = d.get("pseudonym") or ""
        print(f"  {d['wallet'][:14]:<14s}  {d['n_trades']:>6}  {d['n_markets']:>7}  "
              f"${d['total_size_usd']:>12,.0f}  ${d['max_size_usd']:>8,.0f}  {pn[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
