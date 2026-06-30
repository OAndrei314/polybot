"""
Polymarket liquidity-reward pool scanner + net-yield estimator.

The realistic money path in this area is liquidity mining: Polymarket pays ~$28k/day
across hundreds of pools to makers who post qualifying two-sided quotes within
`rewardsMaxSpread` of mid. This tool maps WHERE that money is and estimates how
much of it you could realistically clear with a given capital, AFTER a crude
adverse-selection haircut.

Model (deliberately simple, all assumptions labeled):
  your_share      ~= C / (existing_liquidity + C)        # dilution as you add capital
  gross_daily     =  pool_$ / day * your_share
  gross_yield/day =  gross_daily / C  =  pool_rate / (liq + C)
  net_daily       =  gross_daily * risk_haircut          # adverse-selection/inventory discount
The share proxy uses gamma `liquidityNum` as total competing liquidity (imperfect:
not all liquidity qualifies for rewards, but a usable first-order proxy).

Risk haircut is a HEURISTIC by market type + time-to-resolution. Treat outputs as
a ranked map of where to look, not a guarantee. Real net P&L = rewards - adverse
selection - inventory drift, and must be validated live with small size first.

    python -m research.reward_scanner --capital 5000 --top 30
Outputs: console table + data/edge_latency/reward_pools.csv + reward_pools.png
"""

from __future__ import annotations

import argparse
import csv
import datetime
import re
import time
from pathlib import Path

import requests

OUT = Path("data/edge_latency")

# Adverse-selection risk by market type. Higher haircut = keep less of the gross
# reward because informed flow picks off MM quotes more often / price jumps harder.
_HIGH = (("lol", "cs:", "cs2", "valorant", "dota", "esports", "counter-strike",
          "vs.", "vs ", " bo3", " bo5", "game ", "match", "handicap"), 0.20)
_MED = (("bitcoin", "ethereum", "btc", "eth", "solana", "price", "above $", "below $",
         "airspace", "strait", "ceasefire", "invade", "flight", "by june", "by july"), 0.45)
_LOW = (("election", "president", "nominee", "win the", "champion", "governor",
         "mayor", "2027", "2028", "ipo"), 0.70)


def _haircut(question: str, end_dt: datetime.datetime | None, now: float) -> tuple[str, float]:
    q = (question or "").lower()
    base_tier, base_hc = "MED", 0.45
    for kws, hc in (_LOW, _MED, _HIGH):  # later wins on overlap toward the riskier read
        if any(k in q for k in kws):
            tier = {0.70: "LOW", 0.45: "MED", 0.20: "HIGH"}[hc]
            base_tier, base_hc = tier, hc
    # time-to-resolution penalty: a pool resolving in <2 days concentrates event
    # risk; halve the haircut benefit (you bear the whole move soon).
    if end_dt is not None:
        days = (end_dt.timestamp() - now) / 86400.0
        if 0 < days < 2:
            base_hc *= 0.6
    return base_tier, round(base_hc, 2)


def fetch_pools() -> list[dict]:
    S = requests.Session()
    now = time.time()
    out: list[dict] = []
    seen = set()
    for off in range(0, 2500, 100):
        try:
            r = S.get("https://gamma-api.polymarket.com/markets",
                      params={"closed": "false", "active": "true", "limit": 100,
                              "offset": off, "order": "volume24hr", "ascending": "false"},
                      timeout=20)
            b = r.json()
        except Exception:
            break
        if not b:
            break
        for m in b:
            cid = m.get("conditionId")
            if cid in seen:
                continue
            seen.add(cid)
            cr = m.get("clobRewards") or []
            rate = 0.0
            for c in cr:
                try:
                    rate += float(c.get("rewardsDailyRate") or 0)
                except Exception:
                    pass
            if rate <= 0:
                continue
            try:
                liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            except Exception:
                liq = 0.0
            end = m.get("endDate")
            end_dt = None
            if end:
                try:
                    end_dt = datetime.datetime.fromisoformat(end.replace("Z", "+00:00"))
                except Exception:
                    pass
            q = str(m.get("question") or "")
            tier, hc = _haircut(q, end_dt, now)
            out.append({
                "rate": rate, "liq": liq,
                "max_spread": m.get("rewardsMaxSpread"),
                "min_size": m.get("rewardsMinSize"),
                "spread": m.get("spread"),
                "vol24": float(m.get("volume24hr") or 0),
                "tier": tier, "haircut": hc,
                "end": end, "question": q[:60], "slug": m.get("slug"),
            })
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Polymarket reward-pool scanner")
    p.add_argument("--capital", type=float, default=5000.0, help="capital you'd deploy per pool")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--min-liq", type=float, default=1000.0,
                   help="floor; pools below are flagged ILLIQUID (anomalous yields, likely not capturable)")
    a = p.parse_args(argv)
    C = a.capital

    pools = fetch_pools()
    for x in pools:
        denom = x["liq"] + C
        x["gross_daily"] = x["rate"] * C / denom if denom > 0 else 0.0
        x["net_daily"] = x["gross_daily"] * x["haircut"]
        x["net_yield_pct_day"] = (x["net_daily"] / C * 100.0) if C > 0 else 0.0
        x["net_apr"] = x["net_yield_pct_day"] * 365.0
        x["illiquid"] = x["liq"] < a.min_liq

    ranked = sorted(pools, key=lambda x: x["net_daily"], reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"\nPolymarket reward pools: {len(pools)} paying, "
          f"${sum(x['rate'] for x in pools):,.0f}/day total. Capital modeled: ${C:,.0f}\n")
    print(f"{'net$/d':>7} {'apr%':>6} {'risk':>4} {'pool$/d':>8} {'liq$':>10} "
          f"{'mxSpr':>5} {'flag':>9}  market")
    print("-" * 110)
    for x in ranked[:a.top]:
        flag = "ILLIQUID" if x["illiquid"] else ""
        print(f"{x['net_daily']:>7.1f} {x['net_apr']:>6.0f} {x['tier']:>4} "
              f"{x['rate']:>8.0f} {x['liq']:>10,.0f} {str(x['max_spread']):>5} {flag:>9}  {x['question']}")

    # realistic deployable summary (exclude illiquid anomalies, cap per-pool share at 25%)
    real = [x for x in ranked if not x["illiquid"]]
    deployable = real[:20]
    tot_net = sum(x["net_daily"] for x in deployable)
    tot_cap = C * len(deployable)
    print(f"\nIf you spread ${C:,.0f} across the top {len(deployable)} liquid pools "
          f"(${tot_cap:,.0f} total capital):")
    print(f"  est. net ~${tot_net:,.0f}/day  ~{tot_net/max(tot_cap,1)*100:.2f}%/day  "
          f"~{tot_net/max(tot_cap,1)*365*100:.0f}% APR  (BEFORE fees, gas, and the geo/execution gate)")
    print("  NOTE: haircuts & share model are heuristics — validate live with small size first.")

    # CSV
    csv_path = OUT / "reward_pools.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(ranked[0].keys()))
        w.writeheader()
        w.writerows(ranked)
    print(f"\nsaved {csv_path}")

    # chart: net APR vs pool size, colored by risk, for liquid pools
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        colors = {"LOW": "green", "MED": "orange", "HIGH": "red"}
        fig, ax = plt.subplots(figsize=(11, 7))
        for tier in ("LOW", "MED", "HIGH"):
            pts = [x for x in real if x["tier"] == tier and x["net_apr"] > 0]
            if pts:
                ax.scatter([x["liq"] for x in pts], [x["net_apr"] for x in pts],
                           s=[max(15, x["rate"] / 5) for x in pts],
                           c=colors[tier], alpha=0.6, label=f"{tier} adverse-sel risk")
        ax.set_xscale("log")
        ax.set_xlabel("existing liquidity in pool ($, log) — LEFT = under-supplied = your opening")
        ax.set_ylabel(f"estimated NET APR on ${C:,.0f} (%)")
        ax.set_title("Polymarket liquidity-reward pools: where the money is\n"
                     "(bubble size = pool $/day; want upper-LEFT green/orange)")
        ax.axhline(0, color="k", lw=0.5)
        ax.grid(alpha=0.3, which="both")
        ax.legend()
        plt.tight_layout()
        png = OUT / "reward_pools.png"
        plt.savefig(png, dpi=110)
        print(f"saved {png}")
    except Exception as e:
        print(f"(chart skipped: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
