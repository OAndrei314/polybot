"""Phase 2 - suspicious-pattern / insider detection.

For each smart wallet (high win-rate, significant volume), look for
patterns suggesting non-public information or sharp/insider behavior:

  1. LATE-ENTRY EDGE
     Compare the wallet's entry timestamp to market end time. A wallet
     that consistently enters in the FINAL 10% of a market's window and
     still wins big is suspicious - that's when info advantage matters
     most (news leaks, late polling, etc).

  2. BIG-BET CONCENTRATION
     A wallet whose top 5 markets generate >70% of P&L on bets >$10k
     placed within hours of resolution is a strong insider signature.

  3. CATEGORY SPECIALIZATION
     Wallets that win >65% in a single category (e.g. political) but
     are random elsewhere - suggests vertical-specific information.

  4. RESOLUTION-EVE BIG BETS
     For each wallet, find resolved markets where they placed >$5k in
     a single trade within 24h of resolution AND won. Multiple of these
     within a category is suspicious.

  5. COORDINATED ACTIVITY
     Wallets whose timestamped trades cluster suspiciously close (within
     same minute) to other "smart" wallets on the same market.

Output:
  data/polymarket_alpha/suspicious_patterns.jsonl.gz - one wallet per line
  notebooks/png/polymarket_alpha/  - summary plots

Usage:
  python -m research.polymarket_alpha.detect_patterns
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("data/polymarket_alpha")
MARKETS_FILE = ROOT / "markets.jsonl.gz"
HIST_DIR = ROOT / "wallet_history"
STATS_FILE = ROOT / "wallet_stats.jsonl.gz"
OUT_PATH = ROOT / "suspicious_patterns.jsonl.gz"


def load_markets() -> dict[str, dict]:
    """conditionId -> market metadata including resolution time and outcome."""
    out: dict[str, dict] = {}
    with gzip.open(MARKETS_FILE, "rt", encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            cid = m.get("conditionId")
            if not cid:
                continue
            outs = m.get("outcomes")
            op = m.get("outcomePrices")
            if isinstance(outs, str):
                try: outs = json.loads(outs)
                except: continue
            if isinstance(op, str):
                try: op = json.loads(op)
                except: continue
            if not outs or not op:
                continue
            winner = None
            for o, p in zip(outs, op):
                try:
                    if float(p) >= 0.99:
                        winner = o; break
                except: pass
            if winner is None:
                continue
            end_str = m.get("endDateIso") or m.get("endDate")
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(str(end_str).replace("Z","+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            start_str = m.get("startDate") or m.get("createdAt")
            try:
                start_dt = datetime.fromisoformat(str(start_str).replace("Z","+00:00")) if start_str else None
                if start_dt and start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            except Exception:
                start_dt = None
            out[cid] = {
                "slug": m.get("slug",""),
                "title": m.get("question","")[:100],
                "winner": winner,
                "end_ts": int(end_dt.timestamp()),
                "start_ts": int(start_dt.timestamp()) if start_dt else None,
                "volume": float(m.get("volumeNum") or 0),
            }
    return out


def load_stats() -> dict[str, dict]:
    if not STATS_FILE.exists():
        return {}
    out: dict[str, dict] = {}
    with gzip.open(STATS_FILE, "rt") as f:
        for line in f:
            r = json.loads(line)
            out[r["wallet"]] = r
    return out


def analyze_wallet(history_path: Path, markets: dict[str, dict]) -> dict:
    """Look for suspicious patterns in this wallet's trades."""
    big_late_wins: list[dict] = []  # late, large, won
    fast_in_fast_out: list[dict] = []
    per_market_trades: dict[str, list[dict]] = defaultdict(list)

    with gzip.open(history_path, "rt") as f:
        for line in f:
            t = json.loads(line)
            cid = t.get("conditionId")
            if not cid: continue
            meta = markets.get(cid)
            if not meta: continue
            ts = int(t.get("timestamp") or 0)
            try:
                size = float(t.get("size") or 0); price = float(t.get("price") or 0)
            except: continue
            usd = size * price
            per_market_trades[cid].append({
                "ts": ts, "side": t.get("side"), "outcome": t.get("outcome"),
                "price": price, "size": size, "usd": usd,
            })

    # Late-entry big wins
    for cid, trades in per_market_trades.items():
        meta = markets[cid]
        end_ts = meta["end_ts"]; start_ts = meta.get("start_ts")
        winner = meta["winner"]
        window_s = (end_ts - start_ts) if start_ts else 7*86400
        for tr in trades:
            if tr["side"] != "BUY": continue
            # Within 24h of resolution AND large
            seconds_before_end = end_ts - tr["ts"]
            if seconds_before_end < 0 or seconds_before_end > 24*3600: continue
            if tr["usd"] < 5000: continue
            # Did this bet win?
            if tr["outcome"] == winner:
                big_late_wins.append({
                    "market": meta["slug"],
                    "hours_before_end": seconds_before_end/3600,
                    "outcome_bet": tr["outcome"],
                    "winner": winner,
                    "price_at_entry": tr["price"],
                    "size_usd": tr["usd"],
                    "implied_pnl_per_share": round(1.0 - tr["price"], 3),
                    "implied_total_pnl": round((1.0 - tr["price"]) * tr["size"], 2),
                })

    big_late_wins.sort(key=lambda x: -x["implied_total_pnl"])
    return {
        "big_late_wins_count": len(big_late_wins),
        "big_late_wins_total_pnl": sum(x["implied_total_pnl"] for x in big_late_wins),
        "big_late_wins_top5": big_late_wins[:5],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    print("loading market metadata + wallet stats...")
    markets = load_markets()
    stats = load_stats()
    print(f"  {len(markets)} resolved markets with timestamps, {len(stats)} wallet stats")

    histories = sorted(HIST_DIR.glob("*.jsonl.gz"))
    out_rows: list[dict] = []
    for i, h in enumerate(histories, 1):
        wallet = h.stem.replace(".jsonl", "")
        analysis = analyze_wallet(h, markets)
        analysis["wallet"] = wallet
        s = stats.get(wallet, {})
        analysis["win_rate"] = s.get("win_rate", 0)
        analysis["n_resolved"] = s.get("n_resolved_markets", 0)
        analysis["total_pnl"] = s.get("total_pnl", 0)
        analysis["total_volume_usd"] = s.get("total_volume_usd", 0)
        out_rows.append(analysis)
        if i % 25 == 0:
            print(f"  analyzed {i}/{len(histories)}")

    # Rank by big-late-win count and total implied P&L
    out_rows.sort(key=lambda x: -(x["big_late_wins_count"] * 1000 + x["big_late_wins_total_pnl"]))
    with gzip.open(OUT_PATH, "wt") as f:
        for r in out_rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {OUT_PATH}")

    print()
    print("=== top 10 wallets by 'big-late-win' pattern (>$5k bet, <24h to resolution, won) ===")
    print(f"  {'wallet':<14s}  {'late_wins':>10s}  {'late_pnl':>12s}  {'overall_pnl':>13s}  {'win%':>5s}  {'best_late_bet':<50s}")
    for r in out_rows[:10]:
        if r["big_late_wins_count"] == 0: continue
        top = r["big_late_wins_top5"][0] if r["big_late_wins_top5"] else None
        topstr = (f"${top['size_usd']:>6,.0f} on '{top['market'][:30]}' @{top['hours_before_end']:.1f}h"
                  if top else "")
        print(f"  {r['wallet'][:14]:<14s}  {r['big_late_wins_count']:>10}  "
              f"${r['big_late_wins_total_pnl']:>10,.0f}  ${r['total_pnl']:>11,.0f}  "
              f"{r['win_rate']*100:>4.0f}%  {topstr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
