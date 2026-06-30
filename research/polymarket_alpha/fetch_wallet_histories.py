"""For each discovered wallet, pull their full Polymarket trade history.

Uses data-api `/trades?user=<wallet>` which DOES filter to a single wallet
(unlike the broken `?market=` filter we discovered earlier).

Output: data/polymarket_alpha/wallet_history/<wallet>.jsonl.gz
Resumable: skip wallets already on disk unless --force.

Usage:
  python -m research.polymarket_alpha.fetch_wallet_histories
  python -m research.polymarket_alpha.fetch_wallet_histories --top 50
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path

from research.polymarket_alpha import api


ROOT = Path("data/polymarket_alpha")
WALLETS_FILE = ROOT / "wallets.jsonl.gz"
HIST_DIR = ROOT / "wallet_history"


def load_wallets() -> list[dict]:
    with gzip.open(WALLETS_FILE, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def fetch_full_history(wallet: str, page_size: int = 500,
                       max_pages: int = 200) -> list[dict]:
    """Pull all trades for a wallet, paging until short page or hard cap."""
    out: list[dict] = []
    for page_n in range(max_pages):
        url = f"{api.DATA_BASE}/trades?user={wallet}&limit={page_size}&offset={page_n*page_size}"
        try:
            trades = api.get(url)
        except api.HardClientError:
            break  # offset cap reached
        if not trades:
            break
        out.extend(trades)
        if len(trades) < page_size:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100,
                    help="how many wallets to pull (sorted by total USD volume seen)")
    ap.add_argument("--page-size", type=int, default=500)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    HIST_DIR.mkdir(parents=True, exist_ok=True)
    wallets = load_wallets()[:args.top]
    print(f"pulling full history for {len(wallets)} wallets")

    t_start = time.time()
    total_trades = 0
    for i, w in enumerate(wallets, 1):
        addr = w["wallet"]
        out_path = HIST_DIR / f"{addr}.jsonl.gz"
        if out_path.exists() and not args.force:
            with gzip.open(out_path, "rt") as f:
                n_cached = sum(1 for _ in f)
            total_trades += n_cached
            if i % 25 == 0:
                print(f"  [{i:>3}/{len(wallets)}]  {addr[:14]:<14s}  CACHED ({n_cached} trades)")
            continue
        try:
            trades = fetch_full_history(addr, page_size=args.page_size)
        except Exception as e:
            print(f"  [{i:>3}/{len(wallets)}]  {addr[:14]:<14s}  FAILED: {e}")
            continue
        with gzip.open(out_path, "wt", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")
        total_trades += len(trades)
        elapsed = time.time() - t_start
        rate = i / elapsed if elapsed > 0 else 0
        eta = (len(wallets) - i) / rate if rate > 0 else 0
        if i <= 5 or i % 20 == 0 or i == len(wallets):
            print(f"  [{i:>3}/{len(wallets)}]  {addr[:14]:<14s}  {len(trades):>5} trades   "
                  f"rate={rate:.2f}w/s  eta={eta/60:.1f}min")

    print()
    print(f"done. {total_trades:,} trades total across {len(wallets)} wallets "
          f"in {(time.time()-t_start)/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
