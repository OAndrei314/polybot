"""Page through resolved Polymarket markets, save metadata to disk.

Filters:
  - closed=true (resolved)
  - volumeNum >= MIN_VOLUME (default $10k - skip noise/test markets)
  - endDate within the lookback window

Output: data/polymarket_alpha/markets.jsonl.gz - one market per line.

Usage:
  python -m research.polymarket_alpha.fetch_markets --max-markets 1000
  python -m research.polymarket_alpha.fetch_markets --lookback-days 180 --min-volume 50000
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research.polymarket_alpha import api


OUT_DIR = Path("data/polymarket_alpha")
MARKETS_FILE = OUT_DIR / "markets.jsonl.gz"


def parse_end_dt(m: dict) -> datetime | None:
    for k in ("endDateIso", "endDate"):
        v = m.get(k)
        if v:
            try:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=2000,
                    help="stop after this many resolved markets that pass filters")
    ap.add_argument("--min-volume", type=float, default=10_000.0,
                    help="ignore markets with volumeNum below this (USD)")
    ap.add_argument("--lookback-days", type=int, default=180,
                    help="only keep markets that resolved within this many days")
    ap.add_argument("--page-size", type=int, default=100)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.lookback_days)
    print(f"keeping markets resolved on or after {cutoff.date()} "
          f"with volumeNum >= ${args.min_volume:,.0f}")

    kept: list[dict] = []
    offset = 0
    skipped_low_vol = 0
    skipped_too_old = 0
    pages = 0

    while len(kept) < args.max_markets:
        page = api.gamma_markets(closed=True, limit=args.page_size, offset=offset,
                                 order="volumeNum", ascending=False)
        if not page:
            print(f"  no more results at offset {offset}")
            break
        pages += 1
        for m in page:
            vol = float(m.get("volumeNum") or 0)
            end_dt = parse_end_dt(m)
            if vol < args.min_volume:
                skipped_low_vol += 1
                # Since results are ordered by volume desc, once we drop below
                # min_volume we won't see anything above it again.
                if skipped_low_vol > 50:
                    print(f"  hit run of low-volume markets at vol=${vol:,.0f} -- stopping")
                    break
                continue
            if end_dt is None or end_dt < cutoff:
                skipped_too_old += 1
                continue
            kept.append(m)
            if len(kept) >= args.max_markets:
                break
        else:
            offset += args.page_size
            if pages % 5 == 0:
                print(f"  page {pages}: kept {len(kept)}, skipped low_vol={skipped_low_vol}, too_old={skipped_too_old}")
            continue
        break  # broke from inner loop -> done

    # Write
    with gzip.open(MARKETS_FILE, "wt", encoding="utf-8") as f:
        for m in kept:
            f.write(json.dumps(m) + "\n")
    print(f"\nwrote {len(kept)} markets to {MARKETS_FILE}")
    if kept:
        total_vol = sum(float(m.get("volumeNum") or 0) for m in kept)
        print(f"  total volume covered: ${total_vol:,.0f}")
        print(f"  top 5:")
        for m in kept[:5]:
            print(f"    ${float(m.get('volumeNum') or 0):>14,.0f}  {m.get('slug','')[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
