"""
Thread T: Liquid Restaking Token (LRT) yield + restaking-points/airdrop farming.

A 19th post-A-F follow-up / 24th mechanism tested in the "find a 10%+/month
strategy" objective. Two legs, both quantified with real data:

  1. Base LRT yield (ETH staking pass-through + AVS/restaking rewards visible
     in DeFiLlama's apy field) -- a genuinely new mechanism shape (restaking on
     top of staking) not yet tested in this objective.
  2. Restaking-points-to-token airdrop outcomes (ether.fi/EigenLayer/Renzo/
     Puffer) -- structurally the same "unquantifiable in advance" bimodal
     category as Thread Q (perp-DEX points farming), tested here with the
     4 completed real-world restaking conversions as a base-rate.

No API key needed for either leg (DeFiLlama /pools + /chart/{pool}, CoinGecko
/coins/markets).
"""
import requests

LRT_PROJECTS = {
    "renzo", "swell-earn", "kelp", "swell-liquid-restaking",
    "swell-liquid-staking", "puffer-stake",
}

# pool ids for the 3 largest LRT pools (found via a live /pools pull), used to
# fetch full daily APY history since each pool's DeFiLlama listing date.
HISTORY_POOLS = {
    "kelp RSETH": "33c732f6-a78d-41da-af5b-ccd9fa5e52d5",
    "renzo EZETH": "e28e32b5-e356-41d9-8dc7-a376ece56619",
    "puffer PUFETH": "bac6982a-f344-42f7-9af4-a9882f4a77f0",
}

# CoinGecko ids for the 4 completed restaking-points-to-token conversions.
AIRDROP_TOKENS = ["ether-fi", "eigenlayer", "renzo", "puffer-finance"]


def scan_live_pools():
    r = requests.get("https://yields.llama.fi/pools", timeout=30)
    data = r.json()["data"]
    rows = [p for p in data if p.get("project") in LRT_PROJECTS]
    rows.sort(key=lambda x: -(x.get("tvlUsd") or 0))
    print(f"=== Live LRT pools (n={len(rows)}) ===")
    for p in rows:
        print(
            f"{p['project']:<24} {p['symbol']:<10} {p['chain']:<10} "
            f"TVL=${(p.get('tvlUsd') or 0):>14,.0f}  "
            f"apy={p.get('apy')}%/yr  apyBase={p.get('apyBase')}  apyReward={p.get('apyReward')}"
        )
    return rows


def scan_history():
    print("\n=== Full history (since DeFiLlama listing) of top 3 LRT pools ===")
    for name, pid in HISTORY_POOLS.items():
        r = requests.get(f"https://yields.llama.fi/chart/{pid}", timeout=30)
        d = r.json()["data"]
        apys = [x.get("apy") for x in d if x.get("apy") is not None]
        print(
            f"{name:<16} n={len(d):>4}  since={d[0]['timestamp'][:10]}  "
            f"mean={sum(apys)/len(apys):.2f}%/yr  max={max(apys):.2f}%/yr  "
            f"min={min(apys):.2f}%/yr  live={d[-1].get('apy'):.2f}%/yr"
        )
        print(f"  -> monthly-equivalent (mean): {sum(apys)/len(apys)/12:.3f}%/month, "
              f"(max ever observed): {max(apys)/12:.3f}%/month")


def scan_airdrop_outcomes():
    print("\n=== Real completed restaking-points-to-token conversions ===")
    r = requests.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency": "usd", "ids": ",".join(AIRDROP_TOKENS)},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  CoinGecko returned {r.status_code}, skipping (rate-limited)")
        return
    for c in r.json():
        print(
            f"{c['id']:<16} {c['symbol'].upper():<8} FDV=${(c.get('fully_diluted_valuation') or 0):>14,.0f}  "
            f"mktcap=${(c.get('market_cap') or 0):>14,.0f}  "
            f"ATH_change={c.get('ath_change_percentage'):.1f}%  ATH_date={c.get('ath_date','')[:10]}"
        )


if __name__ == "__main__":
    scan_live_pools()
    scan_history()
    scan_airdrop_outcomes()
