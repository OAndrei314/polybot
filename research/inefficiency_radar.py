"""
Inefficiency radar — find UNDER-SUPPLIED reward pools (the only inefficiency that
is real + exploitable for us; directional mispricing is dead — markets are
efficient/calibrated).

Upgrade over reward_scanner: instead of gamma's `liquidityNum` guess, it fetches
the REAL order book and measures the qualifying competition = resting size WITHIN
`rewardsMaxSpread` of mid (both sides). That is the actual denominator of your
reward share. Adds pool freshness (age) and a model-coverage flag (can we price
the resolver to quote safely?).

Score = estimated net $/day on your capital, using:
  your_share   ~= capital / (in_spread_depth$ + capital)
  gross_daily  =  pool_$/day * your_share
  net_daily    =  gross_daily * adverse_selection_haircut(category, resolution_speed)
Then surfaces fresh + model-able + thin pools to the top.

    python -m research.inefficiency_radar --capital 300 --top 25 --max-books 120
Outputs: console table + data/edge_latency/radar.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime
import time
from pathlib import Path

import requests

OUT = Path("data/edge_latency")
BOOK = "https://clob.polymarket.com/book?token_id={tid}"

# model coverage: can we estimate fair value from a public feed?
_PRICEABLE = ("temperature", "temp ", "weather", "s&p", "spx", "spy", "nasdaq", "dow ",
              "crude", "wti", "brent", "natural gas", "gold", "silver", "bitcoin",
              "ethereum", "btc", "eth", "solana", "cpi", "gdp", "jobs", "fed ",
              "rate", "treasury", "yield", "hit $", "above $", "below $", "closes",
              "up or down", "index", "msft", "aapl", "nvda")
# hard-to-price / news / fast: high adverse selection
_NEWS = ("election", "president", "nominee", " vs.", " vs ", "esports", "lol", "cs:",
         "valorant", "war", "ceasefire", "airspace", "invade", "resign", "fired",
         "court", "indict", "announce", "released by", "launch")


def _jload(x):
    import json
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return None
    return x


def _classify(q: str, hrs_to_end: float | None) -> tuple[bool, str, float]:
    ql = q.lower()
    priceable = any(k in ql for k in _PRICEABLE)
    newsy = any(k in ql for k in _NEWS)
    if newsy and not priceable:
        tier, hc = "HIGH", 0.20
    elif priceable:
        tier, hc = "LOW", 0.65
    else:
        tier, hc = "MED", 0.40
    # fast resolution gaps harder → more adverse selection
    if hrs_to_end is not None and 0 < hrs_to_end < 12:
        hc *= 0.7
        tier += "*"  # mark time-pressured
    return priceable, tier, round(hc, 2)


def _in_spread_depth(tid: str, max_spread_c: float, timeout: float = 7.0) -> tuple[float, float | None]:
    """$ of resting liquidity within rewardsMaxSpread of mid (both sides) + mid."""
    try:
        d = requests.get(BOOK.format(tid=tid), timeout=timeout).json()
    except Exception:
        return -1.0, None
    bids = d.get("bids") or []
    asks = d.get("asks") or []
    bb = max((float(x["price"]) for x in bids), default=None)
    ba = min((float(x["price"]) for x in asks), default=None)
    if bb is None or ba is None:
        return -1.0, None
    mid = (bb + ba) / 2.0
    band = max_spread_c / 100.0
    depth = 0.0
    for x in bids:
        p, s = float(x["price"]), float(x["size"])
        if p >= mid - band:
            depth += p * s
    for x in asks:
        p, s = float(x["price"]), float(x["size"])
        if p <= mid + band:
            depth += p * s
    return depth, mid


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="under-supplied reward-pool radar")
    p.add_argument("--capital", type=float, default=300.0)
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--max-books", type=int, default=120, help="cap real book fetches")
    a = p.parse_args(argv)
    C = a.capital
    S = requests.Session()
    now = time.time()

    # 1) pull reward pools (cheap), pre-rank by gamma reward-density to spend book calls well
    pools = []
    for off in range(0, 2500, 100):
        try:
            b = S.get("https://gamma-api.polymarket.com/markets",
                      params={"closed": "false", "active": "true", "limit": 100,
                              "offset": off, "order": "volume24hr", "ascending": "false"},
                      timeout=20).json()
        except Exception:
            break
        if not b:
            break
        for m in b:
            cr = m.get("clobRewards") or []
            rate = sum(float(c.get("rewardsDailyRate") or 0) for c in cr if c.get("rewardsDailyRate"))
            if rate <= 0:
                continue
            toks = _jload(m.get("clobTokenIds"))
            if not toks:
                continue
            try:
                gliq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            except Exception:
                gliq = 0.0
            end = m.get("endDate")
            hrs = None
            if end:
                try:
                    hrs = (datetime.datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - now) / 3600
                except Exception:
                    pass
            age_h = None
            for k in ("createdAt", "startDate"):
                v = m.get(k)
                if v:
                    try:
                        age_h = (now - datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()) / 3600
                        break
                    except Exception:
                        pass
            pools.append({
                "rate": rate, "gliq": gliq, "yes_tok": str(toks[0]),
                "max_spread": float(m.get("rewardsMaxSpread") or 3.0),
                "min_size": float(m.get("rewardsMinSize") or 50),
                "hrs_to_end": hrs, "age_h": age_h,
                "q": str(m.get("question") or "")[:52],
            })
    # pre-rank by gamma density so the book-fetch budget hits the best candidates
    pools.sort(key=lambda x: x["rate"] / (x["gliq"] + C), reverse=True)
    print(f"reward pools found: {len(pools)}; fetching real books for top {a.max_books}…")

    # 2) refine top candidates with REAL in-spread depth
    refined = []
    for x in pools[:a.max_books]:
        depth, mid = _in_spread_depth(x["yes_tok"], x["max_spread"])
        if depth < 0:
            continue
        priceable, tier, hc = _classify(x["q"], x["hrs_to_end"])
        comp = depth + C
        share = C / comp if comp > 0 else 0
        gross = x["rate"] * share
        net = gross * hc
        refined.append({**x, "in_spread_depth": round(depth, 0), "mid": round(mid, 4) if mid else None,
                        "priceable": priceable, "tier": tier, "haircut": hc,
                        "gross_daily": round(gross, 2), "net_daily": round(net, 2),
                        "net_apr": round((net / C * 365 * 100) if C else 0, 0)})

    refined.sort(key=lambda x: x["net_daily"], reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"\n{'net$/d':>7} {'apr%':>6} {'risk':>5} {'mdl':>3} {'pool$/d':>8} "
          f"{'inSprdDepth$':>12} {'age_h':>6} {'end_h':>6}  market")
    print("-" * 118)
    for x in refined[:a.top]:
        mdl = "Y" if x["priceable"] else "-"
        age = f"{x['age_h']:.1f}" if x["age_h"] is not None else "?"
        endh = f"{x['hrs_to_end']:.0f}" if x["hrs_to_end"] is not None else "?"
        print(f"{x['net_daily']:>7.1f} {x['net_apr']:>6.0f} {x['tier']:>5} {mdl:>3} "
              f"{x['rate']:>8.0f} {x['in_spread_depth']:>12,.0f} {age:>6} {endh:>6}  {x['q']}")

    # the niche we want: model-able (Y), low/med risk, thin in-spread depth, not time-pressured
    niche = [x for x in refined if x["priceable"] and "HIGH" not in x["tier"]
             and x["in_spread_depth"] < 3000 and (x["hrs_to_end"] is None or x["hrs_to_end"] > 12)]
    niche.sort(key=lambda x: x["net_daily"], reverse=True)
    print(f"\n=== BEST FIT (model-able + low/med risk + thin depth + not time-pressured), top 8 ===")
    for x in niche[:8]:
        print(f"  net~${x['net_daily']:.1f}/d  depth=${x['in_spread_depth']:,.0f}  pool=${x['rate']:.0f}/d  "
              f"{x['tier']}  {x['q']}")
    if not niche:
        print("  (none right now — radar is empty; check back as new pools are born)")

    with (OUT / "radar.csv").open("w", newline="", encoding="utf-8") as f:
        if refined:
            w = csv.DictWriter(f, fieldnames=list(refined[0].keys()))
            w.writeheader()
            w.writerows(refined)
    print(f"\nsaved {OUT/'radar.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
