"""
Reward-farming adverse-selection BACKTEST — turns the guessed haircut into a
measured number.

Every prior reward tool (reward_scanner / inefficiency_radar / reward_farmer)
multiplied gross reward income by a HARDCODED adverse-selection haircut
(0.20 / 0.45 / 0.65 by keyword). That guess is the whole ballgame and it was
never measured. This does.

Method (no real trades, public data only):
  For each reward-paying market we pull its realized mid-price path from the CLOB
  prices-history endpoint and REPLAY a two-sided maker:
    - each step, rest bid = mid - delta, ask = mid + delta  (delta within
      rewardsMaxSpread so the quote qualifies for rewards)
    - if the next mid trades through a resting quote, that side fills at our price
      and we carry the inventory; a trending market fills us adversely, an
      oscillating market lets us capture the spread
    - inventory is capped (bankroll protection) and marked at the final price,
      which for resolved markets is the true 0/1 outcome
  The maker's realized P&L (= spread capture - adverse selection) is MEASURED, not
  assumed. Reward income = pool_rate * your_share. We then report the break-even
  share s* = (-trading_pnl/day) / pool_rate: the fraction of the pool you must
  capture just to offset measured adverse selection, and compare it to the share
  your capital could realistically win given live in-spread competition.

    python -m research.reward_backtest --capital 300 --top 25
    python -m research.reward_backtest --capital 300 --include-resolved   # true 0/1 marks
Outputs: console verdict table + data/edge_latency/reward_backtest.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from pathlib import Path

import requests

OUT = Path("data/edge_latency")
GAMMA = "https://gamma-api.polymarket.com/markets"
HIST = "https://clob.polymarket.com/prices-history"
BOOK = "https://clob.polymarket.com/book?token_id={tid}"

# Same priceable niche the radar targets: markets whose fair value tracks a public
# feed and that resolve slowly (the only place a maker can quote safely).
PRICEABLE = ("temperature", "temp ", "weather", "s&p", "spx", "spy", "nasdaq", "dow ",
             "crude", "wti", "brent", "natural gas", "gold", "silver", "bitcoin",
             "ethereum", "btc", "eth", "solana", "cpi", "gdp", "jobs", "fed ",
             "rate", "treasury", "yield", "hit $", "above $", "below $", "closes",
             "up or down", "index", "msft", "aapl", "nvda")


def _jload(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return None
    return x


def _rate(m) -> float:
    return sum(float(c.get("rewardsDailyRate") or 0) for c in (m.get("clobRewards") or []) if c.get("rewardsDailyRate"))


def fetch_pools(S: requests.Session, include_resolved: bool, priceable_only: bool) -> list[dict]:
    pools, seen = [], set()
    closed_vals = ["false", "true"] if include_resolved else ["false"]
    for closed in closed_vals:
        for off in range(0, 2500, 100):
            try:
                b = S.get(GAMMA, params={"closed": closed, "active": "true", "limit": 100,
                                         "offset": off, "order": "volume24hr",
                                         "ascending": "false"}, timeout=20).json()
            except Exception:
                break
            if not b:
                break
            for m in b:
                cid = m.get("conditionId")
                if cid in seen:
                    continue
                rate = _rate(m)
                if rate <= 0:
                    continue
                q = str(m.get("question") or "")
                if priceable_only and not any(k in q.lower() for k in PRICEABLE):
                    continue
                toks = _jload(m.get("clobTokenIds"))
                if not toks:
                    continue
                seen.add(cid)
                pools.append({
                    "q": q, "slug": m.get("slug"), "tok": str(toks[0]), "rate": rate,
                    "max_spread": float(m.get("rewardsMaxSpread") or 3.0),
                    "min_size": float(m.get("rewardsMinSize") or 50),
                    "tick": 0.01 if float(m.get("orderPriceMinTickSize") or 0.01) >= 0.01 else 0.001,
                    "closed": str(m.get("closed")).lower() == "true",
                })
    return pools


def fetch_history(S: requests.Session, tid: str) -> list[tuple[int, float]]:
    try:
        j = S.get(HIST, params={"market": tid, "interval": "max", "fidelity": "1"}, timeout=25).json()
    except Exception:
        return []
    h = j.get("history") if isinstance(j, dict) else j
    if not h:
        return []
    out = []
    for pt in h:
        try:
            out.append((int(pt["t"]), float(pt["p"])))
        except Exception:
            pass
    return out


def in_spread_depth(S: requests.Session, tid: str, max_spread_c: float) -> float | None:
    """$ resting within rewardsMaxSpread of mid (both sides) — the live competition."""
    try:
        d = S.get(BOOK.format(tid=tid), timeout=8).json()
    except Exception:
        return None
    bids, asks = d.get("bids") or [], d.get("asks") or []
    bb = max((float(x["price"]) for x in bids), default=None)
    ba = min((float(x["price"]) for x in asks), default=None)
    if bb is None or ba is None:
        return None
    mid, band = (bb + ba) / 2.0, max_spread_c / 100.0
    depth = sum(float(x["price"]) * float(x["size"]) for x in bids if float(x["price"]) >= mid - band)
    depth += sum(float(x["price"]) * float(x["size"]) for x in asks if float(x["price"]) <= mid + band)
    return depth


def simulate(hist: list[tuple[int, float]], quote_usd: float, inv_cap_usd: float,
             delta: float, pull_thresh: float | None) -> dict:
    """Replay a two-sided maker over the realized mid path with FIXED resting size.

    quote_usd  = $ resting per side (NOT full capital per step — that was the bug).
    inv_cap_usd= hard $ cap on |inventory| (bankroll protection; stop quoting the
                 side that would breach it).
    Returns measured trading P&L, normalized per resting-$ so the verdict is
    size-invariant (reward share and adverse cost both scale with size).
    """
    if len(hist) < 3:
        return {}
    cash = 0.0       # USDC from fills (negative = spent)
    inv = 0.0        # net YES shares held
    fills = 0
    quoting_steps = 0           # steps we actually rested qualifying two-sided quotes
    total_steps = len(hist) - 1
    secs = hist[-1][0] - hist[0][0]
    days = max(secs / 86400.0, 1e-6)
    for i in range(total_steps):
        p0, p1 = hist[i][1], hist[i + 1][1]
        if not (0.02 < p0 < 0.98):
            continue  # tails: maker can't quote a tick off mid meaningfully -> no reward, no fill
        size = max(1.0, quote_usd / max(p0, 0.01))   # FIXED per-side resting shares
        inv_cap_sh = inv_cap_usd / max(p0, 0.01)
        bid, ask = round(p0 - delta, 4), round(p0 + delta, 4)
        # CAUSAL news-pull: react to the PRIOR realized move (known now), not the
        # next one. A maker who just saw a jump widens/pulls before re-quoting.
        if pull_thresh is not None and i > 0 and abs(p0 - hist[i - 1][1]) > pull_thresh:
            continue  # quote pulled this step -> earns NO reward (uptime not counted)
        post_bid = bid > 0 and inv + size <= inv_cap_sh
        post_ask = ask < 1 and inv - size >= -inv_cap_sh
        if post_bid and post_ask:
            quoting_steps += 1     # only two-sided qualifies for the reward pool
        if p1 <= bid and post_bid:            # fell through bid -> we buy
            cash -= bid * size; inv += size; fills += 1
        elif p1 >= ask and post_ask:          # rose through ask -> we sell
            cash += ask * size; inv -= size; fills += 1
    p_final = hist[-1][1]
    trading_pnl = cash + inv * p_final        # mark remaining inventory at final/resolution price
    resting = 2 * quote_usd                   # two-sided resting $ deployed
    uptime = quoting_steps / total_steps if total_steps else 0.0
    return {"days": days, "trading_pnl": trading_pnl, "pnl_per_day": trading_pnl / days,
            "fills": fills, "end_inv": inv, "p_final": p_final, "uptime": uptime,
            "adverse_per_restdollar_day": (-trading_pnl / days) / resting}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="reward-farming adverse-selection backtest")
    p.add_argument("--capital", type=float, default=300.0, help="total bankroll (= inventory $ cap)")
    p.add_argument("--quote-usd", type=float, default=100.0, help="$ resting per side")
    p.add_argument("--top", type=int, default=25, help="markets to simulate (by reward rate)")
    p.add_argument("--pull-thresh", type=float, default=None,
                   help="cancel quotes if realized step move exceeds this (e.g. 0.03); default off (worst case)")
    p.add_argument("--include-resolved", action="store_true",
                   help="also pull recently-resolved markets (true 0/1 terminal marks)")
    p.add_argument("--all-categories", action="store_true",
                   help="don't restrict to the priceable niche")
    a = p.parse_args(argv)
    C = a.capital
    S = requests.Session()

    pools = fetch_pools(S, a.include_resolved, priceable_only=not a.all_categories)
    pools.sort(key=lambda x: x["rate"], reverse=True)
    pools = pools[:a.top]
    print(f"\nsimulating {len(pools)} reward markets @ capital ${C:,.0f} "
          f"(niche={'all' if a.all_categories else 'priceable'}, "
          f"pull_thresh={a.pull_thresh}, resolved={'yes' if a.include_resolved else 'no'})\n")

    rows = []
    for x in pools:
        hist = fetch_history(S, x["tok"])
        if len(hist) < 10:
            continue
        depth = in_spread_depth(S, x["tok"], x["max_spread"])
        band = x["max_spread"] / 100.0
        # Sweep quote width. Tight = full reward score but maximal fills; wide =
        # few fills but reward decays (Polymarket scores tighter quotes higher).
        # reward_weight: ~1 at mid, ->0 at the max-spread edge (linear, conservative).
        deltas = sorted({x["tick"], 2 * x["tick"], round(band / 2, 4), round(band * 0.9, 4)})
        best = None
        for delta in deltas:
            if delta < x["tick"] or delta > band:
                continue
            sim = simulate(hist, a.quote_usd, C, delta, a.pull_thresh)
            if not sim:
                continue
            rw = max(0.0, 1.0 - (delta / band)) if band > 0 else 0.0   # reward spread-utility
            resting = 2 * a.quote_usd
            share = (resting / (depth + resting)) if depth is not None and depth >= 0 else None
            # reward scales with UPTIME: pulled/tail steps earn nothing
            reward_d = (x["rate"] * share * rw * sim["uptime"]) if share is not None else None
            net_d = (reward_d + sim["pnl_per_day"]) if reward_d is not None else None
            cand = {"delta": delta, "rw": rw, "sim": sim, "share": share,
                    "reward_d": reward_d, "net_d": net_d}
            # pick the delta that maximizes net (or, if unpriced, least adverse)
            key = net_d if net_d is not None else sim["pnl_per_day"]
            if best is None or key > (best["net_d"] if best["net_d"] is not None else best["sim"]["pnl_per_day"]):
                best = cand
        if best is None:
            continue
        s, sim = best, best["sim"]
        # adverse cost per resting-$ per day vs reward per resting-$ per day (size-invariant)
        adv_pr = sim["adverse_per_restdollar_day"]            # $ lost per $ resting per day
        rwd_pr = (x["rate"] / depth * s["rw"]) if depth else None  # $ reward per $ resting per day
        rows.append({
            "q": x["q"][:44], "slug": x["slug"], "rate": x["rate"], "days": round(sim["days"], 1),
            "best_delta": s["delta"], "fills": sim["fills"], "p_final": sim["p_final"],
            "mkt_pnl_per_day": round(sim["pnl_per_day"], 3),
            "depth": round(depth) if depth is not None else None,
            "reward_per_day": round(s["reward_d"], 3) if s["reward_d"] is not None else None,
            "net_per_day": round(s["net_d"], 3) if s["net_d"] is not None else None,
            "adv_per_restdollar_day": round(adv_pr, 5),
            "rwd_per_restdollar_day": round(rwd_pr, 5) if rwd_pr is not None else None,
            "rwd_to_adv_ratio": round(rwd_pr / adv_pr, 3) if (rwd_pr is not None and adv_pr > 1e-9) else None,
        })

    rows.sort(key=lambda r: (r["net_per_day"] is not None, r["net_per_day"] or -1e9), reverse=True)
    hdr = (f"{'mkt$/d':>7} {'rwd$/d':>7} {'NET$/d':>7} {'rwd/adv':>7} {'dlt':>5} "
           f"{'fills':>5} {'days':>5}  market")
    print(hdr); print("-" * len(hdr))
    for r in rows:
        def f(v, w, p=2): return (f"{v:>{w}.{p}f}" if isinstance(v, (int, float)) else f"{'?':>{w}}")
        print(f"{f(r['mkt_pnl_per_day'],7)} {f(r['reward_per_day'],7)} {f(r['net_per_day'],7)} "
              f"{f(r['rwd_to_adv_ratio'],7,2)} {f(r['best_delta'],5,3)} "
              f"{r['fills']:>5} {r['days']:>5.0f}  {r['q']}")

    scored = [r for r in rows if r["net_per_day"] is not None]
    if scored:
        pos = [r for r in scored if r["net_per_day"] > 0]
        tot_net = sum(r["net_per_day"] for r in scored)
        ratios = sorted(r["rwd_to_adv_ratio"] for r in scored if r["rwd_to_adv_ratio"] is not None)
        med_ratio = ratios[len(ratios) // 2] if ratios else None
        print(f"\n=== VERDICT (quote ${a.quote_usd:.0f}/side, inv cap ${C:.0f}, best width per mkt) ===")
        print(f"  markets net-positive: {len(pos)}/{len(scored)}")
        print(f"  summed net at realistic share: ${tot_net:,.2f}/day across {len(scored)} markets")
        if med_ratio is not None:
            print(f"  median reward/adverse ratio: {med_ratio:.2f}  "
                  f"(>1 = subsidy beats adverse selection; <1 = dead at ANY size)")
        print(f"  size-invariant: net per resting-$ = rate/depth*rw - adverse_per_$; both legs scale w/ size.")
        print(f"  NOTE: minute-mid fills miss intra-min fills (optimistic); linear reward-decay is")
        print(f"        generous vs Polymarket's actual tighter-is-better scoring. Real <= this.")

    OUT.mkdir(parents=True, exist_ok=True)
    if rows:
        with (OUT / "reward_backtest.csv").open("w", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print(f"\nsaved {OUT/'reward_backtest.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
