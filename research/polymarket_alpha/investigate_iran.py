"""Manual investigation of the Iran-bet cluster.

For each "smart" wallet flagged by Phase 2 (placed >$5k bets within 24h
of resolution on Iran markets), print a chronological timeline of EVERY
bet they made on Iran-related markets. Cross-reference to known events
that may have leaked.

Output: a printable timeline + CSV per wallet.

Usage:
  python -m research.polymarket_alpha.investigate_iran
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("data/polymarket_alpha")
MARKETS_FILE = ROOT / "markets.jsonl.gz"
HIST_DIR = ROOT / "wallet_history"
OUT_DIR = ROOT / "iran_investigation"


# Wallets from the Phase-2 output that stood out on Iran markets.
TARGET_WALLETS = [
    ("0xb10047d6a254b2ebb306d7a7d13bf59171ab6461", "consistent winner, $126k overall, 66% wr"),
    ("0x12d6cccfc7470a3f4bafc53599a4779cbf2cf2a8", "consistent, $23k overall, 63% wr"),
    ("0xbacd00c9080a82ded56f504ee8810af732b0ab35", "8 big-late wins but $-453k overall - mixed signal"),
    ("0x963137a0ba0b02ebf6f37710023cc1b0303289a1", "76% wr, $28k, all on Iran"),
]

IRAN_PATTERN = re.compile(r"iran|nuclear|tehran|imminent|airspace|hormuz|ayatollah|persian", re.I)


def load_markets() -> dict[str, dict]:
    out = {}
    with gzip.open(MARKETS_FILE, "rt", encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            cid = m.get("conditionId")
            slug = m.get("slug", "")
            if not cid: continue
            if not IRAN_PATTERN.search(slug): continue
            outs = m.get("outcomes")
            op = m.get("outcomePrices")
            if isinstance(outs, str):
                try: outs = json.loads(outs)
                except: continue
            if isinstance(op, str):
                try: op = json.loads(op)
                except: continue
            if not outs or not op: continue
            winner = None
            for o, p in zip(outs, op):
                try:
                    if float(p) >= 0.99: winner = o; break
                except: pass
            if winner is None: continue
            end_str = m.get("endDateIso") or m.get("endDate")
            try:
                end_dt = datetime.fromisoformat(str(end_str).replace("Z","+00:00"))
                if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=timezone.utc)
            except: continue
            start_str = m.get("startDate") or m.get("createdAt")
            try:
                start_dt = datetime.fromisoformat(str(start_str).replace("Z","+00:00")) if start_str else None
                if start_dt and start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            except: start_dt = None
            out[cid] = {
                "slug": slug,
                "title": (m.get("question") or "")[:140],
                "winner": winner,
                "end_ts": int(end_dt.timestamp()),
                "start_ts": int(start_dt.timestamp()) if start_dt else None,
                "volume": float(m.get("volumeNum") or 0),
            }
    return out


def wallet_iran_timeline(wallet: str, iran_markets: dict[str, dict]) -> list[dict]:
    path = HIST_DIR / f"{wallet}.jsonl.gz"
    if not path.exists():
        return []
    trades = []
    with gzip.open(path, "rt") as f:
        for line in f:
            t = json.loads(line)
            cid = t.get("conditionId")
            if cid not in iran_markets: continue
            meta = iran_markets[cid]
            try:
                sz = float(t.get("size") or 0); px = float(t.get("price") or 0)
            except: continue
            ts = int(t.get("timestamp") or 0)
            hours_before_end = (meta["end_ts"] - ts) / 3600.0 if ts else None
            outcome = t.get("outcome")
            side = t.get("side")
            usd = sz * px
            # Did this individual bet win?
            won = (side == "BUY" and outcome == meta["winner"]) or \
                  (side == "SELL" and outcome != meta["winner"])
            trades.append({
                "ts": ts,
                "dt_utc": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
                "market_slug": meta["slug"],
                "market_title": meta["title"],
                "winner": meta["winner"],
                "side": side,
                "outcome": outcome,
                "price": round(px, 4),
                "size": round(sz, 2),
                "usd": round(usd, 2),
                "hours_before_end": round(hours_before_end, 2) if hours_before_end is not None else None,
                "won_this_bet": won,
            })
    trades.sort(key=lambda x: x["ts"])
    return trades


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("loading Iran-related resolved markets...")
    iran = load_markets()
    print(f"  found {len(iran)} Iran-related resolved markets")
    for cid, m in sorted(iran.items(), key=lambda x: -x[1]["volume"])[:8]:
        print(f"    ${m['volume']:>14,.0f}  winner={m['winner']:<5s}  {m['slug'][:55]}")

    print()
    for wallet, note in TARGET_WALLETS:
        timeline = wallet_iran_timeline(wallet, iran)
        out_csv = OUT_DIR / f"{wallet[:14]}_iran_timeline.csv"
        if timeline:
            with out_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(timeline[0].keys()))
                w.writeheader(); w.writerows(timeline)

        # Per-wallet summary
        n_won = sum(1 for t in timeline if t["won_this_bet"])
        late_big = [t for t in timeline if t["usd"] >= 5000 and t["hours_before_end"] is not None and 0 <= t["hours_before_end"] <= 24]
        late_big_wins = [t for t in late_big if t["won_this_bet"]]
        early_big = [t for t in timeline if t["usd"] >= 5000 and t["hours_before_end"] is not None and t["hours_before_end"] > 72]
        early_big_wins = [t for t in early_big if t["won_this_bet"]]

        print()
        print(f"=== {wallet[:14]} ({note}) ===")
        print(f"  Iran trades: {len(timeline)}  individual-bet win rate: {n_won}/{len(timeline)}  "
              f"({100*n_won/max(len(timeline),1):.1f}%)")
        print(f"  big late-window bets (>=$5k, <=24h to resolution): {len(late_big)}  wins: {len(late_big_wins)}")
        print(f"  big early-window bets (>=$5k, >72h to resolution): {len(early_big)}  wins: {len(early_big_wins)}")

        # Print top 5 most striking individual bets
        striking = sorted(
            (t for t in timeline if t["usd"] >= 2000),
            key=lambda x: (-x["usd"], x.get("hours_before_end") or 9999),
        )[:8]
        if striking:
            print(f"  most striking bets (top 8 by USD):")
            print(f"    {'datetime UTC':<20s}  {'usd':>9s}  {'price':>6s}  {'side':<4s}  {'outcome':<8s}  "
                  f"{'hrs_before':>10s}  {'won':<3s}  market")
            for t in striking:
                print(f"    {t['dt_utc']:<20s}  ${t['usd']:>7,.0f}  {t['price']:>6.3f}  {t['side']:<4s}  "
                      f"{(t['outcome'] or '')[:7]:<8s}  {t['hours_before_end']:>10}  "
                      f"{'YES' if t['won_this_bet'] else 'NO ':<3s}  {t['market_slug'][:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
