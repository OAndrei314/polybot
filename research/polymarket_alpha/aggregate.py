"""Aggregate per-wallet trade history into win/loss stats per resolved market.

For each (wallet, conditionId) we compute:
  - total_cost     = sum of BUY price * size
  - total_proceeds = sum of SELL price * size
  - net_position   = buy_size - sell_size, per outcome (Yes side / No side)
  - payout         = net_position * (1 if that outcome won else 0)
  - realized_pnl   = total_proceeds + payout - total_cost

A position is a "win" if realized_pnl > 0.

Per-wallet aggregates:
  - n_resolved_markets    (number of markets we can score)
  - n_wins, n_losses
  - win_rate              (wins / (wins+losses))
  - total_pnl
  - total_volume          (USD turnover)
  - categories            (rough categorization from market slug)

Output: data/polymarket_alpha/wallet_stats.jsonl.gz - one wallet per line.

Usage:
  python -m research.polymarket_alpha.aggregate
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path("data/polymarket_alpha")
MARKETS_FILE = ROOT / "markets.jsonl.gz"
HIST_DIR = ROOT / "wallet_history"
STATS_FILE = ROOT / "wallet_stats.jsonl.gz"


CATEGORY_PATTERNS = [
    ("political", re.compile(r"trump|biden|kamala|harris|election|presidential|president|congress|senate|governor|gop|democrat|republican|romanian|german|french|uk-|polls", re.I)),
    ("crypto",    re.compile(r"bitcoin|btc|ethereum|eth|crypto|solana|doge|memecoin|defi|coinbase|binance", re.I)),
    ("sports",    re.compile(r"nba|nfl|mlb|nhl|epl|ufc|tennis|football|basketball|hockey|soccer|world-cup|finals|playoff|grand-slam|atp|wta|cs2|csgo|dota|valorant", re.I)),
    ("geopolitics", re.compile(r"iran|israel|ukraine|russia|china|ceasefire|war|nuclear|north-korea|nato|treaty", re.I)),
    ("macro",     re.compile(r"fed|interest-rates|rate-cut|cpi|inflation|recession|gdp|unemployment", re.I)),
    ("tech",      re.compile(r"openai|chatgpt|elon|musk|tesla|spacex|apple|google|amazon|meta|microsoft|nvidia", re.I)),
    ("entertainment", re.compile(r"oscar|grammy|emmy|movie|netflix|spotify|album|song|book", re.I)),
]


def categorize(slug: str) -> str:
    for name, pat in CATEGORY_PATTERNS:
        if pat.search(slug):
            return name
    return "other"


def load_resolved_markets() -> dict[str, dict]:
    """conditionId -> market metadata (with outcomes + outcomePrices)."""
    by_cid: dict[str, dict] = {}
    with gzip.open(MARKETS_FILE, "rt", encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            cid = m.get("conditionId")
            if not cid:
                continue
            # outcomePrices comes as a JSON-encoded string ('["1.0","0.0"]')
            op = m.get("outcomePrices")
            if isinstance(op, str):
                try:
                    op = json.loads(op)
                except Exception:
                    op = None
            outs = m.get("outcomes")
            if isinstance(outs, str):
                try:
                    outs = json.loads(outs)
                except Exception:
                    outs = None
            if not op or not outs or len(op) != len(outs):
                continue
            try:
                op_f = [float(x) for x in op]
            except Exception:
                continue
            # Identify winner: outcome with price ~= 1
            winner = None
            for o, p in zip(outs, op_f):
                if p >= 0.99:
                    winner = o
                    break
            if winner is None:
                continue
            by_cid[cid] = {
                "slug": m.get("slug",""),
                "title": m.get("question",""),
                "outcomes": outs,
                "winner": winner,
                "category": categorize(m.get("slug","")),
                "volume": float(m.get("volumeNum") or 0),
                "endDate": m.get("endDateIso") or m.get("endDate"),
            }
    return by_cid


def score_wallet(history_path: Path, markets: dict[str, dict]) -> dict:
    """Aggregate a wallet's trade history vs resolved markets."""
    by_market: dict[str, dict] = defaultdict(lambda: {
        "cost": 0.0, "proceeds": 0.0,
        "pos": defaultdict(float),  # outcome -> net shares (BUY - SELL)
    })
    n_total_trades = 0
    total_volume_usd = 0.0
    with gzip.open(history_path, "rt", encoding="utf-8") as f:
        for line in f:
            t = json.loads(line)
            n_total_trades += 1
            cid = t.get("conditionId")
            side = t.get("side")
            outcome = t.get("outcome")
            try:
                sz = float(t.get("size") or 0); px = float(t.get("price") or 0)
            except Exception:
                continue
            usd = sz * px
            total_volume_usd += usd
            if not cid or not side or not outcome:
                continue
            d = by_market[cid]
            if side == "BUY":
                d["cost"] += usd
                d["pos"][outcome] += sz
            else:  # SELL
                d["proceeds"] += usd
                d["pos"][outcome] -= sz

    n_resolved = 0
    n_wins = 0
    n_losses = 0
    total_pnl = 0.0
    cat_counts: dict[str, int] = defaultdict(int)
    cat_pnl: dict[str, float] = defaultdict(float)
    best_market: tuple[float, str] | None = None
    worst_market: tuple[float, str] | None = None

    for cid, d in by_market.items():
        meta = markets.get(cid)
        if not meta:
            continue
        winner = meta["winner"]
        payout = d["pos"].get(winner, 0.0)  # paid 1.0 per net long share of winner
        # Net P&L = proceeds (sells) + payout (winning longs) - cost (buys)
        # If net pos in winner is NEGATIVE (net short winner), we owe shares -> payout is negative
        pnl = d["proceeds"] + payout - d["cost"]
        # For losing-side shorts: any net negative position on losing outcomes settled at 0
        # so no additional cash flow; already handled by proceeds.
        # Skip markets where wallet is at exactly 0 position with 0 volume
        if abs(d["cost"]) + abs(d["proceeds"]) < 1.0:
            continue
        n_resolved += 1
        if pnl > 0: n_wins += 1
        elif pnl < 0: n_losses += 1
        total_pnl += pnl
        cat = meta["category"]
        cat_counts[cat] += 1
        cat_pnl[cat] += pnl
        slug = meta["slug"]
        if best_market is None or pnl > best_market[0]:
            best_market = (pnl, slug)
        if worst_market is None or pnl < worst_market[0]:
            worst_market = (pnl, slug)

    return {
        "n_total_trades": n_total_trades,
        "n_resolved_markets": n_resolved,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "win_rate": n_wins / max(n_wins + n_losses, 1),
        "total_pnl": round(total_pnl, 2),
        "total_volume_usd": round(total_volume_usd, 2),
        "best_market_pnl": round(best_market[0], 2) if best_market else 0,
        "best_market_slug": best_market[1] if best_market else "",
        "worst_market_pnl": round(worst_market[0], 2) if worst_market else 0,
        "worst_market_slug": worst_market[1] if worst_market else "",
        "categories_by_count": dict(cat_counts),
        "categories_by_pnl": {k: round(v, 2) for k, v in cat_pnl.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    print("loading resolved markets...")
    markets = load_resolved_markets()
    print(f"  {len(markets)} resolved markets with known winner")

    histories = sorted(HIST_DIR.glob("*.jsonl.gz"))
    print(f"scoring {len(histories)} wallets...")

    out_rows = []
    for i, h in enumerate(histories, 1):
        wallet = h.stem.replace(".jsonl", "")
        stats = score_wallet(h, markets)
        stats["wallet"] = wallet
        out_rows.append(stats)
        if i % 20 == 0 or i == len(histories):
            print(f"  scored {i}/{len(histories)}")

    out_rows.sort(key=lambda x: -x["total_pnl"])
    with gzip.open(STATS_FILE, "wt", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {STATS_FILE}")

    # Summary
    print()
    print("=== top 15 by realized P&L on resolved markets ===")
    print(f"  {'wallet':<14s}  {'trades':>7s}  {'resolved':>9s}  {'W/L':>10s}  "
          f"{'win%':>5s}  {'P&L':>14s}  {'volume':>14s}  top_cat")
    for r in out_rows[:15]:
        wl = f"{r['n_wins']}/{r['n_losses']}"
        top_cat = max(r["categories_by_count"].items(), key=lambda kv: kv[1])[0] if r["categories_by_count"] else "-"
        print(f"  {r['wallet'][:14]:<14s}  {r['n_total_trades']:>7}  {r['n_resolved_markets']:>9}  "
              f"{wl:>10s}  {r['win_rate']*100:>4.0f}%  ${r['total_pnl']:>12,.0f}  "
              f"${r['total_volume_usd']:>12,.0f}  {top_cat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
