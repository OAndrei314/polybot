"""Live copy-trade tracker for pre-registered out-of-sample test.

Per cron tick:
  1. For each TARGET_WALLET, fetch their most recent trades via data-api.
  2. Compare to what we've already recorded. Any NEW BUYs are appended to
     the tracker journal (data/polymarket_alpha/copy_tracker/<wallet>.jsonl).
  3. For each tracked trade whose market has now resolved:
       - look up the market outcome from gamma-api
       - compute our copy P&L (1/10 size, 2% fees, 0.005 slippage) vs theirs
       - update tracker journal entry with the resolved fields
  4. Print summary of running tally per wallet.

Cron suggestion: every 30 min via Windows Task Scheduler. Each tick is
~5 seconds of API work.

Pre-registration: data/experiments/copy_trade_live_prereg.md (locked
before any new data was observed).

Outputs:
  data/polymarket_alpha/copy_tracker/<wallet>.jsonl        per-trade journal
  data/polymarket_alpha/copy_tracker/summary.json          rolling tally

Usage:
  python -m research.polymarket_alpha.live_copy_tracker
"""

from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from research.polymarket_alpha import api


ROOT = Path("data/polymarket_alpha")
TRACKER_DIR = ROOT / "copy_tracker"
TRACKER_DIR.mkdir(parents=True, exist_ok=True)

# Set on first journal write per wallet so we can distinguish baseline
# (recorded in the first tick = historical) from genuine OOS trades.
TRACKER_LOCK_FILE = TRACKER_DIR / "tracker_lock_ts.json"

# Locked targets from the prereg.
TARGET_WALLETS = [
    "0x9495425feeb0",  # we'll match by prefix to handle the short form
    "0x963137a0ba0b",
]

# Locked simulation parameters (must match copy_backtest.py).
COPY_FRACTION = 0.1
FEE_RATE = 0.02
SLIPPAGE = 0.005


def find_full_address(prefix: str) -> str | None:
    """Map the 14-char prefix to the full 0x… address from our wallet stats."""
    stats_file = ROOT / "wallet_stats.jsonl.gz"
    if not stats_file.exists():
        return None
    with gzip.open(stats_file, "rt") as f:
        for line in f:
            d = json.loads(line)
            w = d.get("wallet", "")
            if w.startswith(prefix):
                return w
    return None


def load_journal(wallet: str) -> list[dict]:
    path = TRACKER_DIR / f"{wallet}.jsonl"
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip(): continue
            e = json.loads(line)
            # Migration: pre-fix entries lack is_baseline. They were captured
            # in the first tracker tick (historical) so flag them baseline.
            if "is_baseline" not in e:
                e["is_baseline"] = True
            out.append(e)
    return out


def save_journal(wallet: str, journal: list[dict]) -> None:
    path = TRACKER_DIR / f"{wallet}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for entry in journal:
            f.write(json.dumps(entry) + "\n")


def market_resolution(condition_id: str) -> dict | None:
    """Look up market resolution from CLOB API.

    NOTE: gamma-api's ?conditionIds= filter is silently broken - it ignores
    the conditionId and returns an arbitrary market. The CLOB API endpoint
    /markets/<condition_id> is the working lookup. Resolution is in
    tokens[*].winner (boolean), not in outcomePrices like gamma reports.
    """
    url = f"https://clob.polymarket.com/markets/{condition_id}"
    try:
        # use_cache=False because cached open-market responses (closed=False)
        # become stale forever once the market closes. Tracker runs every 30
        # min so the polite-throttle cost is fine.
        data = api.get(url, use_cache=False)
    except Exception:
        return None
    if not data:
        return None
    # CLOB returns a single market dict, not a list
    m = data if isinstance(data, dict) else (data[0] if data else None)
    if not m or not m.get("closed"):
        return None
    tokens = m.get("tokens") or []
    winner = next((t.get("outcome") for t in tokens if t.get("winner")), None)
    if winner is None:
        return None
    return {
        "closed": True,
        "winner": winner,
        "slug": m.get("market_slug", ""),
        "title": (m.get("question") or "")[:140],
    }


def compute_our_pnl(entry_price: float, size: float, won: bool) -> float:
    """Our 1/10 mirror P&L net of slippage and fees on entry + payout."""
    our_size = size * COPY_FRACTION
    our_entry_px = min(0.999, entry_price + SLIPPAGE)
    our_cost = our_size * our_entry_px
    fee_entry = our_cost * FEE_RATE
    if won:
        payout = our_size  # $1 per share
        fee_payout = payout * FEE_RATE
        return payout - fee_payout - our_cost - fee_entry
    else:
        return -(our_cost + fee_entry)


def their_pnl(entry_price: float, size: float, won: bool) -> float:
    """Their gross P&L (no fees deducted - we don't know their tier)."""
    if won:
        return size * (1.0 - entry_price)
    else:
        return -size * entry_price


def _get_or_set_lock_ts(wallet: str) -> int:
    """When was this wallet first tracked? Used to label baseline vs OOS."""
    locks: dict[str, int] = {}
    if TRACKER_LOCK_FILE.exists():
        try:
            locks = json.loads(TRACKER_LOCK_FILE.read_text())
        except Exception:
            locks = {}
    if wallet not in locks:
        locks[wallet] = int(datetime.now(timezone.utc).timestamp())
        TRACKER_LOCK_FILE.write_text(json.dumps(locks, indent=2))
    return locks[wallet]


def tick(wallet: str) -> dict:
    """One poll cycle for a single wallet.

    Records BOTH BUYs and SELLs so net per-market positions can be computed
    at resolution time (mirrors the in-sample backtest methodology, which
    is the only correct way to score a hedging trader's P&L).
    """
    journal = load_journal(wallet)
    seen_tx = {(e["timestamp"], e["transactionHash"], e["asset"]) for e in journal}
    lock_ts = _get_or_set_lock_ts(wallet)
    is_first_tick = len(journal) == 0
    # Pull the wallet's most recent trades
    fresh = api.get(f"{api.DATA_BASE}/trades?user={wallet}&limit=500",
                    use_cache=False)
    new_count = 0
    for t in fresh or []:
        # Track BUY and SELL both so we can net positions per market.
        if t.get("side") not in ("BUY", "SELL"): continue
        key = (t.get("timestamp"), t.get("transactionHash"), t.get("asset"))
        if key in seen_tx: continue
        try:
            sz = float(t.get("size") or 0); px = float(t.get("price") or 0)
        except: continue
        # Baseline = anything captured in the first tick (historical) OR any
        # trade whose own timestamp predates the tracker lock. OOS = anything
        # placed by the wallet AFTER we started watching them.
        trade_ts = int(t.get("timestamp") or 0)
        baseline = is_first_tick or (trade_ts < lock_ts)
        entry = {
            "wallet": wallet,
            "timestamp": trade_ts,
            "dt_utc": datetime.fromtimestamp(trade_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if trade_ts else "",
            "transactionHash": t.get("transactionHash"),
            "conditionId": t.get("conditionId"),
            "asset": t.get("asset"),
            "outcome": t.get("outcome"),
            "side": t.get("side"),
            "price": px,
            "size": sz,
            "usd": round(sz * px, 2),
            "title": (t.get("title") or "")[:120],
            "slug": t.get("slug",""),
            "is_baseline": baseline,
            "recorded_at_ts": int(time.time()),
            # filled in later when market resolves
            "resolved": False,
            "winner": None,
        }
        journal.append(entry)
        new_count += 1

    # Now check each unresolved market for resolution status. We cache the
    # resolution per conditionId (not per entry) so we don't redo lookups.
    unresolved_cids = {e["conditionId"] for e in journal
                       if not e.get("resolved") and e.get("conditionId")}
    resolved_count = 0
    cid_to_winner: dict[str, str] = {}
    for cid in unresolved_cids:
        meta = market_resolution(cid)
        if not meta: continue
        cid_to_winner[cid] = meta["winner"]
    for entry in journal:
        if entry.get("resolved"): continue
        winner = cid_to_winner.get(entry.get("conditionId"))
        if winner is None: continue
        entry["resolved"] = True
        entry["winner"] = winner
        resolved_count += 1

    save_journal(wallet, journal)
    return {
        "new": new_count,
        "newly_resolved": resolved_count,
        "total_tracked": len(journal),
        "total_resolved": sum(1 for e in journal if e.get("resolved")),
    }


def _score_position(entries: list[dict]) -> tuple[float, float, int, int]:
    """Net-position P&L for one (market, outcome) bucket.

    Mirrors the in-sample backtest:
      - their_cost = sum(BUY price*size)
      - their_proceeds = sum(SELL price*size) + (net_position * 1.0 if won)
      - their_pnl = proceeds - cost
      - our_pnl: 1/10 size, slippage on BUYs, 2% fee on entry + payout
    Returns (their_pnl, our_pnl, n_winning_positions, n_total_positions).
    """
    if not entries:
        return 0.0, 0.0, 0, 0
    winner = entries[0]["winner"]  # all share the same market resolution
    outcome = entries[0]["outcome"]
    won = (outcome == winner)
    # Their math
    their_cost = sum(e["price"] * e["size"] for e in entries if e["side"] == "BUY")
    their_proceeds = sum(e["price"] * e["size"] for e in entries if e["side"] == "SELL")
    net_position = (sum(e["size"] for e in entries if e["side"] == "BUY")
                    - sum(e["size"] for e in entries if e["side"] == "SELL"))
    if net_position > 0 and won:
        their_proceeds += net_position  # $1/share payout
    their_pnl = their_proceeds - their_cost
    # Our copy: mirror BUYs only (we have no position to SELL)
    our_cost = 0.0
    our_long = 0.0
    for e in entries:
        if e["side"] != "BUY": continue
        our_sz = e["size"] * COPY_FRACTION
        our_px = min(0.999, e["price"] + SLIPPAGE)
        usd = our_sz * our_px
        fee = usd * FEE_RATE
        our_cost += usd + fee
        our_long += our_sz
    our_proceeds = 0.0
    if our_long > 0 and won:
        payout = our_long
        our_proceeds = payout - payout * FEE_RATE
    our_pnl = our_proceeds - our_cost
    return their_pnl, our_pnl, 1 if their_pnl > 0 else 0, 1


def summarize_journal(wallet: str) -> dict:
    journal = load_journal(wallet)
    lock_ts = _get_or_set_lock_ts(wallet)

    # Group by (cid, outcome). Compute scores separately for baseline vs OOS.
    def group_and_score(entries: list[dict]) -> dict:
        by_pos: dict[tuple, list[dict]] = defaultdict(list)
        for e in entries:
            if not e.get("resolved"): continue
            by_pos[(e["conditionId"], e["outcome"])].append(e)
        their_total = 0.0; our_total = 0.0; wins = 0; n = 0
        for pos_entries in by_pos.values():
            their, ours, w, k = _score_position(pos_entries)
            their_total += their; our_total += ours; wins += w; n += k
        scaled_their = their_total * COPY_FRACTION
        return {
            "n_resolved_positions": n,
            "n_winning_positions": wins,
            "n_losing_positions": n - wins,
            "win_rate": wins / max(n, 1),
            "their_pnl": round(their_total, 2),
            "scaled_their_pnl": round(scaled_their, 2),
            "our_pnl_net": round(our_total, 2),
            "live_efficiency": round(our_total / scaled_their, 3) if scaled_their > 0 else None,
        }

    baseline_entries = [e for e in journal if e.get("is_baseline")]
    oos_entries = [e for e in journal if not e.get("is_baseline")]
    baseline_score = group_and_score(baseline_entries)
    oos_score = group_and_score(oos_entries)

    return {
        "wallet": wallet,
        "tracker_lock_ts": lock_ts,
        "n_tracked": len(journal),
        "n_baseline_entries": len(baseline_entries),
        "n_oos_entries": len(oos_entries),
        "baseline": baseline_score,
        "oos": oos_score,
    }


def main() -> int:
    summaries = []
    for prefix in TARGET_WALLETS:
        full = find_full_address(prefix)
        if not full:
            print(f"  could not resolve {prefix} to full address (wallet_stats missing?)")
            continue
        t = tick(full)
        s = summarize_journal(full)
        s.update({"address": full, "this_tick": t})
        summaries.append(s)

    # Write summary
    with (TRACKER_DIR / "summary.json").open("w") as f:
        json.dump({
            "ts": datetime.now(timezone.utc).isoformat(),
            "wallets": summaries,
        }, f, indent=2)

    # Print: split baseline (historical = in-sample-ish) vs OOS (post-lock)
    print(f"[{datetime.now(timezone.utc).isoformat()}] copy tracker tick")
    for s in summaries:
        t = s["this_tick"]
        b = s["baseline"]; o = s["oos"]
        b_eff = f"{b['live_efficiency']:.2f}x" if b['live_efficiency'] is not None else "n/a"
        o_eff = f"{o['live_efficiency']:.2f}x" if o['live_efficiency'] is not None else "n/a"
        print(f"  {s['address'][:14]:<14s}  baseline: pos={b['n_resolved_positions']:<3} "
              f"W/L={b['n_winning_positions']}/{b['n_losing_positions']:<3} "
              f"our=${b['our_pnl_net']:+8,.2f} eff={b_eff}")
        print(f"  {'':<14s}  OOS:      pos={o['n_resolved_positions']:<3} "
              f"W/L={o['n_winning_positions']}/{o['n_losing_positions']:<3} "
              f"our=${o['our_pnl_net']:+8,.2f} eff={o_eff}    "
              f"(this tick: +{t['new']} new trades, +{t['newly_resolved']} resolved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
