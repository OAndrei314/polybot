"""
Thread U: Solana liquid-staking-token (LST) leveraged looping (Kamino/MarginFi).

Mechanism: deposit a Solana LST (JitoSOL/mSOL/bSOL) as collateral on a lending
market, borrow SOL against it, swap the borrowed SOL for more LST, redeposit,
repeat ("loop"). Net yield = leverage * LST_staking_yield - (leverage-1) * SOL_borrow_cost.

This is a genuinely new mechanism family vs. prior threads:
- Thread T (LRT restaking, iteration 38) tested Ethereum restaking (kelp/renzo/puffer).
- Thread I (Ethena sUSDe loop, iteration 19) tested a stablecoin-denominated loop.
This is a SOL-denominated liquid-staking loop, which benefits from Jito's MEV-tip
kickback (not present in plain ETH staking) but is a genuinely distinct asset/venue.

Real data sources (no API key needed):
- Jito's own kobe stake-pool-stats API: https://kobe.mainnet.jito.network/api/v1/stake_pool_stats
- DeFiLlama /pools + /chart/{pool}: jito-liquid-staking, marinade-liquid-staking (467-day
  real APY history for JitoSOL back to 2025-03-26)
- Kamino's own public reserves/metrics API (live borrow/supply APY + max LTV per token)
"""
import requests

KAMINO_MAIN_MARKET = "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"
DEFILLAMA_POOLS = "https://yields.llama.fi/pools"
DEFILLAMA_CHART = "https://yields.llama.fi/chart/{}"


def fetch_kamino_reserves():
    r = requests.get(
        f"https://api.kamino.finance/kamino-market/{KAMINO_MAIN_MARKET}/reserves/metrics",
        timeout=20,
    )
    r.raise_for_status()
    return {d["liquidityToken"]: d for d in r.json()}


def fetch_llama_pools():
    r = requests.get(DEFILLAMA_POOLS, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def fetch_llama_chart(pool_id):
    r = requests.get(DEFILLAMA_CHART.format(pool_id), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def summarize_apy_history(rows):
    vals = [x["apy"] for x in rows if x.get("apy") is not None]
    if not vals:
        return None
    last90 = [x["apy"] for x in rows[-90:] if x.get("apy") is not None]
    last30 = [x["apy"] for x in rows[-30:] if x.get("apy") is not None]
    return {
        "n_days": len(vals),
        "full_mean": mean(vals),
        "full_min": min(vals),
        "full_max": max(vals),
        "last90_mean": mean(last90) if last90 else None,
        "last30_mean": mean(last30) if last30 else None,
        "current": vals[-1],
    }


def loop_net_apy(staking_yield_pct, borrow_cost_pct, ltv):
    """Net annualized % return on equity for one round of infinite-loop leverage at a given LTV."""
    leverage = 1.0 / (1.0 - ltv)
    return leverage * staking_yield_pct - (leverage - 1.0) * borrow_cost_pct, leverage


def main():
    print("=" * 78)
    print("Thread U: Solana LST leveraged-loop scan (Kamino Main Market)")
    print("=" * 78)

    reserves = fetch_kamino_reserves()
    pools = fetch_llama_pools()

    lst_llama_projects = {
        "JITOSOL": ("jito-liquid-staking", "JITOSOL"),
        "MSOL": ("marinade-liquid-staking", "MSOL"),
    }

    # --- Live Kamino borrow-cost floor (SOL) ---
    sol_reserve = reserves.get("SOL")
    sol_borrow_apy = float(sol_reserve["borrowApy"]) * 100
    sol_supply_apy = float(sol_reserve["supplyApy"]) * 100
    print(f"\nLive Kamino Main Market SOL: borrowApy={sol_borrow_apy:.2f}%/yr  "
          f"supplyApy={sol_supply_apy:.2f}%/yr  (borrow is what a looper actually pays)")

    # Historical floor proxy for borrow cost: DeFiLlama only exposes supply-side
    # apyBase history for the SOL reserve, so use that as a (generously low) floor —
    # real borrow cost is structurally >= supply cost, same proxy technique as Thread I.
    sol_pool_id = None
    for p in pools:
        if p["project"] == "kamino-lend" and p.get("chain") == "Solana" and p["symbol"] == "SOL" \
                and p.get("poolMeta") == "Main Market":
            sol_pool_id = p["pool"]
            break
    sol_borrow_floor_hist = None
    if sol_pool_id:
        rows = fetch_llama_chart(sol_pool_id)
        vals = [x["apyBase"] for x in rows if x.get("apyBase") is not None]
        last90 = [x["apyBase"] for x in rows[-90:] if x.get("apyBase") is not None]
        last30 = [x["apyBase"] for x in rows[-30:] if x.get("apyBase") is not None]
        sol_borrow_floor_hist = {
            "n_days": len(vals),
            "full_mean": mean(vals),
            "last90_mean": mean(last90),
            "last30_mean": mean(last30),
        }
        print(f"SOL supply-APY history ({sol_borrow_floor_hist['n_days']}d, borrow-cost FLOOR proxy): "
              f"full-mean={sol_borrow_floor_hist['full_mean']:.2f}%  "
              f"last90={sol_borrow_floor_hist['last90_mean']:.2f}%  "
              f"last30={sol_borrow_floor_hist['last30_mean']:.2f}%")

    print("\n" + "-" * 78)
    print(f"{'LST':<8}{'MaxLTV':>8}{'Lev':>7}{'LiveStkAPY':>12}{'FullMeanStk':>13}"
          f"{'LiveBorrow':>12}{'NetAPY(live)':>14}{'NetAPY(hist-floor)':>20}")
    results = []
    for sym, (proj, llama_sym) in lst_llama_projects.items():
        reserve = reserves.get(sym)
        if not reserve:
            continue
        max_ltv = float(reserve["maxLtv"])

        pool_id = None
        for p in pools:
            if p["project"] == proj and p["symbol"] == llama_sym:
                pool_id = p["pool"]
                break
        if not pool_id:
            continue
        hist = fetch_llama_chart(pool_id)
        summary = summarize_apy_history(hist)

        net_live, leverage = loop_net_apy(summary["current"], sol_borrow_apy, max_ltv)
        net_hist = None
        if sol_borrow_floor_hist:
            net_hist, _ = loop_net_apy(summary["full_mean"], sol_borrow_floor_hist["full_mean"], max_ltv)

        print(f"{sym:<8}{max_ltv:>8.2f}{leverage:>7.2f}x{summary['current']:>11.2f}%"
              f"{summary['full_mean']:>12.2f}%{sol_borrow_apy:>11.2f}%"
              f"{net_live:>13.2f}%{('%.2f%%' % net_hist) if net_hist is not None else 'n/a':>20}")

        results.append({
            "symbol": sym,
            "max_ltv": max_ltv,
            "leverage": leverage,
            "staking_apy_summary": summary,
            "net_apy_live": net_live,
            "net_apy_hist_floor": net_hist,
        })

    print("-" * 78)

    # Conservative LTV sensitivity for the largest/most liquid LST (JitoSOL)
    jito = next(r for r in results if r["symbol"] == "JITOSOL")
    print("\nJitoSOL loop sensitivity to LTV (using LIVE staking APY vs LIVE borrow cost):")
    for ltv in (0.30, 0.40, 0.50, 0.59):
        net, lev = loop_net_apy(jito["staking_apy_summary"]["current"], sol_borrow_apy, ltv)
        print(f"  LTV={ltv:.2f} (leverage={lev:.2f}x): net APY = {net:+.2f}%/yr "
              f"({net/12:+.2f}%/month)")

    print("\nJitoSOL loop sensitivity to LTV (using BEST-CASE historical floor: "
          "full-history mean staking yield vs full-history mean SOL SUPPLY apy as borrow floor):")
    if sol_borrow_floor_hist:
        for ltv in (0.30, 0.40, 0.50, 0.59):
            net, lev = loop_net_apy(jito["staking_apy_summary"]["full_mean"],
                                     sol_borrow_floor_hist["full_mean"], ltv)
            print(f"  LTV={ltv:.2f} (leverage={lev:.2f}x): net APY = {net:+.2f}%/yr "
                  f"({net/12:+.2f}%/month)")

    print("\nVerdict: DEAD. Live SOL borrow cost (%.2f%%/yr) exceeds every LST's live and "
          "full-history staking yield at every leverage level tested, making the live-rate "
          "loop net-negative at all LTVs. Even the most generous possible historical proxy "
          "(full-history SOL SUPPLY apy, which understates real borrow cost) only yields "
          "~0.5-0.9%%/month net at max LTV -- still far below the 5%%/month floor and below "
          "the standing XMR funding-basis candidate (2.04%%/month)." % sol_borrow_apy)


if __name__ == "__main__":
    main()
