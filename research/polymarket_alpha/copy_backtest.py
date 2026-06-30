"""Backtest copy-trading the top smart wallets at fractional size.

For each smart wallet, we simulate that we had been watching their bets in
real time and immediately mirrored each BUY at 1/N size, then realized P&L
at market resolution.

Models:
  - copy_delay_s: assume we see+execute the trade with this latency.
    During the delay the price can drift; we assume +0.5 cent slippage.
  - fee_rate: 2% taker fee on entry, 2% on payout at resolution.
  - copy_fraction: e.g. 1/10 = bet $100 when they bet $1000.

For each wallet we report:
  - their realized P&L (theirs)
  - our copy P&L (after fees, slippage)
  - copy efficiency = our P&L / their P&L * copy_fraction

If efficiency > 1.0 net of fees, copying is profitable. If < 0, copying
loses money even when they win (because frictions dominate small gains).

Usage:
  python -m research.polymarket_alpha.copy_backtest
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path("data/polymarket_alpha")
MARKETS_FILE = ROOT / "markets.jsonl.gz"
HIST_DIR = ROOT / "wallet_history"
STATS_FILE = ROOT / "wallet_stats.jsonl.gz"


def load_markets() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with gzip.open(MARKETS_FILE, "rt") as f:
        for line in f:
            m = json.loads(line)
            cid = m.get("conditionId")
            if not cid: continue
            outs = m.get("outcomes"); op = m.get("outcomePrices")
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
            out[cid] = {"slug": m.get("slug",""), "winner": winner}
    return out


def simulate_copy(history_path: Path, markets: dict[str, dict],
                  copy_fraction: float, fee_rate: float,
                  slippage: float) -> dict:
    """Walk a wallet's BUY trades, mirror each at copy_fraction size."""
    their_cost = 0.0
    their_payout = 0.0
    our_cost = 0.0
    our_payout = 0.0
    n_their_trades = 0
    n_our_trades = 0
    n_winning_their = 0
    n_winning_our = 0

    # Track per-(market, outcome) net positions for accurate payout calc
    their_pos: dict[tuple[str, str], float] = defaultdict(float)
    our_pos: dict[tuple[str, str], float] = defaultdict(float)
    their_market_cost: dict[str, float] = defaultdict(float)
    their_market_proceeds: dict[str, float] = defaultdict(float)
    our_market_cost: dict[str, float] = defaultdict(float)
    our_market_proceeds: dict[str, float] = defaultdict(float)

    with gzip.open(history_path, "rt") as f:
        for line in f:
            t = json.loads(line)
            cid = t.get("conditionId")
            outcome = t.get("outcome")
            side = t.get("side")
            if not cid or not outcome or not side: continue
            if cid not in markets: continue
            try:
                sz = float(t.get("size") or 0); px = float(t.get("price") or 0)
            except: continue
            usd = sz * px
            n_their_trades += 1

            # Their math (no fees in their trade)
            if side == "BUY":
                their_market_cost[cid] += usd
                their_pos[(cid, outcome)] += sz
            else:
                their_market_proceeds[cid] += usd
                their_pos[(cid, outcome)] -= sz

            # Our copy: only mirror BUYs (we don't have a position to SELL)
            if side == "BUY":
                # Slipped entry price + taker fee
                our_px = min(0.999, px + slippage)
                our_sz = sz * copy_fraction
                our_usd = our_sz * our_px
                fee = our_usd * fee_rate
                our_market_cost[cid] += our_usd + fee
                our_pos[(cid, outcome)] += our_sz
                n_our_trades += 1

    # Resolve at market end
    for (cid, outcome), pos in their_pos.items():
        if cid not in markets: continue
        winner = markets[cid]["winner"]
        if outcome == winner and pos > 0:
            their_market_proceeds[cid] += pos  # winning shares pay $1 each

    for (cid, outcome), pos in our_pos.items():
        if cid not in markets: continue
        winner = markets[cid]["winner"]
        if outcome == winner and pos > 0:
            payout = pos
            fee = payout * fee_rate
            our_market_proceeds[cid] += payout - fee

    for cid in set(their_market_cost) | set(their_market_proceeds):
        pnl = their_market_proceeds.get(cid, 0) - their_market_cost.get(cid, 0)
        if pnl > 0: n_winning_their += 1
        their_cost += their_market_cost.get(cid, 0)
        their_payout += their_market_proceeds.get(cid, 0)
    for cid in set(our_market_cost) | set(our_market_proceeds):
        pnl = our_market_proceeds.get(cid, 0) - our_market_cost.get(cid, 0)
        if pnl > 0: n_winning_our += 1
        our_cost += our_market_cost.get(cid, 0)
        our_payout += our_market_proceeds.get(cid, 0)

    their_pnl = their_payout - their_cost
    our_pnl = our_payout - our_cost
    return {
        "their_trades_n": n_their_trades,
        "our_trades_n": n_our_trades,
        "their_winning_markets": n_winning_their,
        "our_winning_markets": n_winning_our,
        "their_pnl": round(their_pnl, 2),
        "our_pnl": round(our_pnl, 2),
        "their_cost": round(their_cost, 2),
        "our_cost": round(our_cost, 2),
        "scaled_their_pnl": round(their_pnl * copy_fraction, 2),
        "copy_efficiency": round(our_pnl / max(their_pnl * copy_fraction, 1e-9), 3) if their_pnl > 0 else 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy-fraction", type=float, default=0.1,
                    help="we bet this fraction of their size (default 0.1 = 1/10)")
    ap.add_argument("--fee-rate", type=float, default=0.02,
                    help="taker fee on entry and on payout (default 0.02)")
    ap.add_argument("--slippage", type=float, default=0.005,
                    help="USD per share of entry-price slip due to copy latency (default 0.5 cent)")
    ap.add_argument("--top-n", type=int, default=15,
                    help="how many wallets to simulate (sorted by their P&L)")
    args = ap.parse_args()

    print("loading markets and stats...")
    markets = load_markets()
    with gzip.open(STATS_FILE, "rt") as f:
        stats = [json.loads(l) for l in f if l.strip()]
    stats.sort(key=lambda x: -x.get("total_pnl", 0))
    top = stats[:args.top_n]
    print(f"  {len(markets)} resolved markets, simulating top {len(top)} wallets")
    print(f"  copy_fraction={args.copy_fraction}  fee_rate={args.fee_rate}  slippage=${args.slippage}/share")
    print()
    print(f"  {'wallet':<14s}  {'their_PnL':>11s}  {'scaled':>10s}  {'our_PnL':>10s}  "
          f"{'efficiency':>11s}  {'their_W':>8s}  {'our_W':>6s}  {'our_trades':>11s}  category_top")

    total_our_pnl = 0.0
    total_scaled_their = 0.0
    for s in top:
        wallet = s["wallet"]
        h = HIST_DIR / f"{wallet}.jsonl.gz"
        if not h.exists(): continue
        r = simulate_copy(h, markets, args.copy_fraction, args.fee_rate, args.slippage)
        cats = s.get("categories_by_count", {})
        top_cat = max(cats.items(), key=lambda kv: kv[1])[0] if cats else "-"
        total_our_pnl += r["our_pnl"]
        total_scaled_their += r["scaled_their_pnl"]
        print(f"  {wallet[:14]:<14s}  ${r['their_pnl']:>9,.0f}  ${r['scaled_their_pnl']:>8,.0f}  "
              f"${r['our_pnl']:>+8,.0f}  {r['copy_efficiency']:>10.2f}x  {r['their_winning_markets']:>8}  "
              f"{r['our_winning_markets']:>6}  {r['our_trades_n']:>11}  {top_cat}")

    print()
    print(f"=== aggregate (mirroring top {len(top)} wallets at {args.copy_fraction:.0%} size) ===")
    print(f"  their pnl summed (scaled to copy size): ${total_scaled_their:,.2f}")
    print(f"  OUR pnl summed (after fees+slippage):   ${total_our_pnl:+,.2f}")
    if total_scaled_their > 0:
        overall_eff = total_our_pnl / total_scaled_their
        print(f"  overall copy efficiency: {overall_eff:.2%}")
        if total_our_pnl > 0:
            print(f"  VERDICT: copying these wallets at 1/{int(1/args.copy_fraction)} size would have netted +${total_our_pnl:,.0f}")
        else:
            print(f"  VERDICT: copying these wallets LOST money after fees+slippage. The edge dies in frictions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
