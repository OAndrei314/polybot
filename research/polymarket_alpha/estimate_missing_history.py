"""Estimate how much wallet history we're missing due to the 3500-cap.

Polymarket's data-api hard-caps pagination at offset 3500 on every endpoint
(/trades, /activity). The Graph's free hosted service was sunset 2024 -- its
gateway now requires a paid API key. So our per-wallet history is bounded
at 3500 trades.

This script uses the free Polygonscan API (no key required at low rates)
to count each top wallet's TOTAL on-chain transactions vs. how many we
have. The gap is what we're missing.

Note: this gives transactions, not trades. A trade = 1+ tx (USDC approval +
order match). So we divide by ~2 for a rough trade estimate.

Usage:
  python -m research.polymarket_alpha.estimate_missing_history
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.request
from pathlib import Path


# Polygonscan free API key. Set via env var POLYGONSCAN_API_KEY, or paste
# directly here. Get a free key at https://polygonscan.com/myapikey
# (signup is ~30 sec, no credit card; free tier = 5 req/sec, 100k/day).
POLYGONSCAN_API_KEY = os.environ.get("POLYGONSCAN_API_KEY", "").strip()


ROOT = Path("data/polymarket_alpha")
STATS_FILE = ROOT / "wallet_stats.jsonl.gz"
HIST_DIR = ROOT / "wallet_history"


def polygonscan_tx_count(addr: str) -> int | None:
    """Approximate count via paginated tx list (free, rate-limited).

    Requires POLYGONSCAN_API_KEY env var (or constant above). Without one,
    you'll get NOTOK responses.
    """
    if not POLYGONSCAN_API_KEY:
        return None
    url = (f"https://api.polygonscan.com/api?module=account&action=txlist"
           f"&address={addr}&startblock=0&endblock=99999999&page=1&offset=10000"
           f"&sort=desc&apikey={POLYGONSCAN_API_KEY}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polybot-research/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if data.get("status") == "1":
            return len(data.get("result", []))
        return None
    except Exception:
        return None


def main() -> int:
    with gzip.open(STATS_FILE, "rt") as f:
        stats = [json.loads(l) for l in f if l.strip()]
    stats.sort(key=lambda x: -x.get("total_volume_usd", 0))
    top = stats[:15]
    print(f"checking on-chain tx count for top {len(top)} wallets by USD volume")
    print(f"  comparison: how many trades we have in cache vs. their total chain activity")
    print()
    print(f"  {'wallet':<14s}  {'our_trades':>10s}  {'onchain_tx':>11s}  {'coverage':>10s}  pseudonym")

    for s in top:
        addr = s["wallet"]
        h = HIST_DIR / f"{addr}.jsonl.gz"
        if not h.exists():
            continue
        with gzip.open(h, "rt") as f:
            our_n = sum(1 for _ in f)
        tx_n = polygonscan_tx_count(addr)
        if tx_n is None:
            cov = "?"
            tx_str = "?"
        else:
            tx_str = f"{tx_n}{'+' if tx_n >= 10000 else ''}"
            # Rough estimate: ~2 tx per trade (approval + match), so trades ~ tx/2
            est_trades = tx_n / 2
            if est_trades > 0:
                cov = f"{min(100, 100 * our_n / est_trades):.0f}%"
            else:
                cov = "n/a"
        # be polite to polygonscan free tier
        time.sleep(0.25)
        print(f"  {addr[:14]:<14s}  {our_n:>10}  {tx_str:>11s}  {cov:>10s}")

    print()
    print("Reads:")
    print("  - Wallets where coverage is ~100%: we have everything, no missing data")
    print("  - Wallets where coverage is <50%: our sample is the recent tail only")
    print("  - Wallets where tx count is 10000+: at Polygonscan's per-page cap, real number higher")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
