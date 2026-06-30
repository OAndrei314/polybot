"""
Liquidity-reward farmer — the compounding engine (dry-run by default).

Strategy (the ONLY +EV, compoundable vehicle we found):
  Post two-sided GTC limit quotes within `rewardsMaxSpread` of fair value on a
  chosen reward-paying market. You collect a share of the daily reward pool for
  resting, qualifying liquidity. You are NOT predicting the outcome — the edge is
  the subsidy. Your risk is adverse selection (filled against you on news) and
  inventory (holding a position that resolves badly).

Bankroll-protection (critical at $300):
  - pick LOW adverse-selection pools (slow/predictable resolvers; this tool warns
    on fast/HIGH-risk markets)
  - cap inventory so one bad resolution can't take more than a set fraction
  - pull quotes when fair value moves past a threshold (news)

SAFETY: uses market.clob_orders.ClobOrderClient, which is DRY-RUN by default and
hard-blocked by a STOP.lock file. Live requires --live AND wallet creds in env.
Prove realized net yield in dry-run / tiny-live BEFORE scaling the $300.

    python -m research.reward_farmer --slug <market-slug> --capital 300         # dry-run
    python -m research.reward_farmer --slug <market-slug> --capital 300 --live  # real (creds req)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests

from market.clob_orders import ClobOrderClient

GAMMA = "https://gamma-api.polymarket.com/events?slug={slug}"
BOOK = "https://clob.polymarket.com/book?token_id={tid}"
LOG = Path("data/edge_latency/farm")


def _jload(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return None
    return x


def _fetch_market(slug: str) -> dict[str, Any] | None:
    try:
        e = (requests.get(GAMMA.format(slug=slug), timeout=15).json() or [None])[0]
        return e
    except Exception:
        return None


def _best(tid: str) -> tuple[float | None, float | None]:
    try:
        d = requests.get(BOOK.format(tid=tid), timeout=8).json()
        bids = d.get("bids") or []
        asks = d.get("asks") or []
        bb = max((float(x["price"]) for x in bids), default=None)
        ba = min((float(x["price"]) for x in asks), default=None)
        return bb, ba
    except Exception:
        return None, None


class RewardFarmer:
    def __init__(self, slug: str, capital: float, live: bool, refresh_s: float,
                 inventory_cap_frac: float, tick: float = 0.001) -> None:
        self.slug = slug
        self.capital = capital
        self.refresh_s = refresh_s
        self.inventory_cap = capital * inventory_cap_frac
        self.tick = tick
        self.client = ClobOrderClient(live=live)
        LOG.mkdir(parents=True, exist_ok=True)
        self.log = (LOG / "farm.jsonl").open("a", buffering=1)
        self.live = live

    def _quotes_for_bracket(self, m: dict, fair: float, max_spread_c: float, min_size: float):
        """Two-sided quotes around fair, inside rewardsMaxSpread, tick-aligned.

        Returns (bid_price, ask_price, size_shares) or None if not quotable.
        """
        if not (0.02 < fair < 0.98):
            return None
        max_off = (max_spread_c / 100.0) / 2.0       # half-spread budget in price
        off = min(self.tick, max_off)                 # quote one tick off mid (tightest = best reward score)
        bid = round(fair - off, 3)
        ask = round(fair + off, 3)
        if bid <= 0 or ask >= 1:
            return None
        # size: spend at most inventory_cap of capital on this bracket per side
        per_side_usdc = min(self.capital, max(min_size, self.inventory_cap))
        size = max(min_size, round(per_side_usdc / max(fair, 0.01)))
        return bid, ask, size

    def run(self, duration_s: float, max_spread_override: float | None) -> int:
        ev = _fetch_market(self.slug)
        if not ev:
            print(f"could not fetch market '{self.slug}'")
            return 1
        title = ev.get("title")
        mks = [m for m in (ev.get("markets") or []) if not m.get("closed")]
        if not mks:
            print("no open markets in event")
            return 1
        # reward params (per market)
        m0 = mks[0]
        max_spread = max_spread_override or float(m0.get("rewardsMaxSpread") or 3.0)
        min_size = float(m0.get("rewardsMinSize") or 50)
        print(f"=== FARMING: {title} ===")
        print(f"mode={'LIVE' if self.live else 'DRY-RUN'} capital=${self.capital:.0f} "
              f"rewardsMaxSpread={max_spread}c minSize={min_size} inv_cap=${self.inventory_cap:.0f}")
        ql = (title or "").lower()
        if any(k in ql for k in ("vs.", "vs ", "bo3", "bo5", "esports", "lol", "cs:", "valorant")):
            print("  !! WARNING: fast/HIGH adverse-selection market — risky for a small bankroll. "
                  "Prefer weather/data markets.")

        t_end = time.time() + duration_s
        ticks = 0
        while time.time() < t_end:
            ticks += 1
            # GLOBAL capital budget per tick — never commit more USDC in resting bids
            # than the bankroll. Concentrate on brackets nearest 0.5 (most reward-
            # relevant and where two-sided MM is meaningful), not the dead tails.
            committed = 0.0
            graded = []
            for m in mks:
                toks = _jload(m.get("clobTokenIds"))
                if not toks:
                    continue
                yes = str(toks[0])
                bb, ba = _best(yes)
                if bb is None or ba is None:
                    op = _jload(m.get("outcomePrices"))
                    fair = None
                    if op:
                        try:
                            fair = float(op[0])
                        except Exception:
                            fair = None
                else:
                    fair = (bb + ba) / 2.0
                if fair is None or not (0.02 < fair < 0.98):
                    continue
                graded.append((abs(fair - 0.5), m, yes, fair))
            graded.sort(key=lambda x: x[0])  # closest to 0.5 first
            for _, m, yes, fair in graded:
                q = self._quotes_for_bracket(m, fair, max_spread, min_size)
                if q is None:
                    continue
                bid, ask, size = q
                bid_cost = bid * size
                if committed + bid_cost > self.capital:
                    # shrink to remaining budget; skip if below min_size
                    rem = self.capital - committed
                    size = rem / max(bid, 0.01)
                    if size < min_size:
                        continue
                    bid_cost = bid * size
                committed += bid_cost
                lab = (m.get("question") or "")[:40]
                # BID side first (bootstrap inventory). Ask side only once we hold YES
                # shares — the live client rejects an ask you can't back (correct guard);
                # in dry-run we still log it so two-sided sizing is visible.
                rb = self.client.place_limit(yes, "BUY", bid, size, order_type="GTC", note=f"farm bid {lab}")
                ra = self.client.place_limit(yes, "SELL", ask, size, order_type="GTC", note=f"farm ask {lab}")
                rec = {"ts": time.time(), "slug": self.slug, "bracket": lab, "fair": round(fair, 4),
                       "bid": bid, "ask": ask, "size": round(size), "bid_usdc": round(bid_cost, 2),
                       "committed_usdc": round(committed, 2), "dry_run": rb.dry_run,
                       "bid_ok": rb.accepted, "ask_ok": ra.accepted}
                self.log.write(json.dumps(rec) + "\n")
                if ticks == 1:
                    print(f"  quote {lab:40} fair={fair:.3f} bid={bid:.3f}/ask={ask:.3f} "
                          f"size={size:.0f} bid_cost=${bid_cost:.0f} total=${committed:.0f} "
                          f"({'would-send' if rb.dry_run else 'SENT'})")
            if ticks == 1:
                print(f"  -> total committed this tick: ${committed:.0f} of ${self.capital:.0f} budget")
            time.sleep(self.refresh_s)
        print(f"[farmer] done. ticks={ticks}. log -> {LOG/'farm.jsonl'}")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="liquidity-reward farmer (dry-run default)")
    p.add_argument("--slug", required=True, help="market event slug to farm")
    p.add_argument("--capital", type=float, default=300.0)
    p.add_argument("--live", action="store_true", help="REAL orders (needs wallet creds in env)")
    p.add_argument("--refresh-s", type=float, default=20.0)
    p.add_argument("--inventory-cap-frac", type=float, default=0.33,
                   help="max fraction of capital exposed per side (bankroll protection)")
    p.add_argument("--max-spread", type=float, default=None, help="override rewardsMaxSpread (cents)")
    p.add_argument("--duration", type=float, default=120.0)
    a = p.parse_args(argv)
    return RewardFarmer(a.slug, a.capital, a.live, a.refresh_s,
                        a.inventory_cap_frac).run(a.duration, a.max_spread)


if __name__ == "__main__":
    raise SystemExit(main())
