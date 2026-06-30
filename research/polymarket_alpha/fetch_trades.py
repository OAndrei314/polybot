"""For each market in markets.jsonl.gz, fetch the full trade history.

Polymarket's data-api supports `?market=<slug>&limit=...&offset=...`. We
page until we get fewer than `limit` results (end of history).

A `min_trade_size` filter skips the noise of micro-trades; we only really
care about wallets placing serious bets (smart money signature).

Output: data/polymarket_alpha/trades/<slug>.jsonl.gz - one trade per line.
Resumable: existing files are skipped unless --force.

Usage:
  python -m research.polymarket_alpha.fetch_trades --max-markets 100 --min-size 100
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path

from research.polymarket_alpha import api


ROOT = Path("data/polymarket_alpha")
MARKETS_FILE = ROOT / "markets.jsonl.gz"
TRADES_DIR = ROOT / "trades"


def load_markets() -> list[dict]:
    with gzip.open(MARKETS_FILE, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def fetch_market_trades(slug: str, min_size: float = 0.0,
                        max_pages: int = 200, page_size: int = 500) -> list[dict]:
    """Page through all trades on a market, filter by min_size."""
    out: list[dict] = []
    offset = 0
    for page_n in range(max_pages):
        trades = api.data_trades_for_market(slug, limit=page_size, offset=offset)
        if not trades:
            break
        for t in trades:
            sz = float(t.get("size") or 0)
            if sz >= min_size:
                out.append(t)
        if len(trades) < page_size:
            break  # last page
        offset += page_size
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=100,
                    help="how many markets to fetch trades for (top by volume)")
    ap.add_argument("--min-size", type=float, default=100.0,
                    help="skip trades smaller than this (shares, not USD)")
    ap.add_argument("--max-pages", type=int, default=400,
                    help="safety cap on pagination per market")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch markets already on disk")
    args = ap.parse_args()

    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    markets = load_markets()[:args.max_markets]
    print(f"fetching trades for {len(markets)} markets")
    print(f"  min_size filter: {args.min_size} (shares)")
    print(f"  cached files in {TRADES_DIR}")
    print()

    t_start = time.time()
    total_trades = 0
    for i, m in enumerate(markets, 1):
        slug = m.get("slug") or ""
        if not slug:
            continue
        out_path = TRADES_DIR / f"{slug}.jsonl.gz"
        if out_path.exists() and not args.force:
            with gzip.open(out_path, "rt") as f:
                n_cached = sum(1 for _ in f)
            total_trades += n_cached
            if i % 25 == 0 or i <= 3:
                print(f"  [{i:>4}/{len(markets)}]  {slug[:60]:<60s}  CACHED ({n_cached} trades)")
            continue
        try:
            trades = fetch_market_trades(slug, min_size=args.min_size,
                                         max_pages=args.max_pages)
        except Exception as e:
            print(f"  [{i:>4}/{len(markets)}]  {slug[:60]:<60s}  FAILED: {e}")
            continue
        with gzip.open(out_path, "wt", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t) + "\n")
        total_trades += len(trades)
        elapsed = time.time() - t_start
        rate = i / elapsed if elapsed > 0 else 0
        eta = (len(markets) - i) / rate if rate > 0 else 0
        print(f"  [{i:>4}/{len(markets)}]  {slug[:60]:<60s}  {len(trades):>5} trades   "
              f"rate={rate:.1f}mkt/s  eta={eta/60:.1f}min")

    elapsed = time.time() - t_start
    print()
    print(f"done. {total_trades:,} trades across {len(markets)} markets in {elapsed/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
