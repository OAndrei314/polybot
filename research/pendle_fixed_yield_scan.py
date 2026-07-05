"""
Thread P (post-A-F follow-up #14): Pendle Finance fixed-yield PT-token markets.

Pendle splits a yield-bearing token into a Principal Token (PT, redeemable 1:1
for the underlying at a fixed maturity date) and a Yield Token (YT, which
captures all yield until maturity). Buying PT at a discount to the underlying
and holding to maturity locks in a FIXED annualized rate ("impliedApy") --
this is a genuinely different mechanism than every prior thread in this
objective (all of which harvest a *variable* rate): it's a zero-coupon-bond
style fixed-rate lock-in, quotable and tradable today with a known payoff at
a known date (assuming no de-peg/default of the underlying asset).

This has not been tested in any prior iteration of this objective.

Data sources (all real, live, no API key required):
- https://api-v2.pendle.finance/core/v1/{chainId}/markets/active
  -> every currently active PT/YT market per chain, with live impliedApy,
     liquidity (USD TVL), and expiry
- https://api-v2.pendle.finance/core/v2/{chainId}/markets/{address}/apy-history?time_frame=day
  -> full daily history of impliedApy since market creation, used to check
     whether today's snapshot is a sustained rate or a momentary spike
     (same spike-detector methodology used in iterations 7, 15, 19, 23, 24)
"""
import requests
import time
from datetime import datetime, timezone

CHAINS = {1: "Ethereum", 42161: "Arbitrum", 8453: "Base", 56: "BSC"}
MIN_LIQUIDITY_USD = 200_000
TOP_N_FOR_HISTORY = 8


def fetch_active_markets():
    all_markets = []
    for chain_id, chain_name in CHAINS.items():
        try:
            r = requests.get(
                f"https://api-v2.pendle.finance/core/v1/{chain_id}/markets/active",
                timeout=20,
            )
            if r.status_code != 200:
                print(f"  [{chain_name}] status {r.status_code}, skipping")
                continue
            markets = r.json().get("markets", [])
            for m in markets:
                m["_chain_id"] = chain_id
                m["_chain_name"] = chain_name
            all_markets.extend(markets)
            print(f"  [{chain_name}] {len(markets)} active markets")
        except Exception as e:
            print(f"  [{chain_name}] error: {e}")
    return all_markets


def fetch_apy_history(chain_id, address):
    try:
        r = requests.get(
            f"https://api-v2.pendle.finance/core/v2/{chain_id}/markets/{address}/apy-history",
            params={"time_frame": "day"},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        rows = data.get("results", "")
        lines = rows.strip().split("\n")[1:]  # skip header
        parsed = []
        for line in lines:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            ts, underlying_apy, implied_apy = parts[0], parts[1], parts[2]
            try:
                parsed.append((int(ts), float(underlying_apy), float(implied_apy)))
            except ValueError:
                continue
        return parsed
    except Exception:
        return None


def main():
    print("=" * 100)
    print("THREAD P: Pendle Finance fixed-yield PT-token scan (live data)")
    print("=" * 100)
    print(f"\nRun timestamp (UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"Chains queried: {list(CHAINS.values())}")
    print(f"Liquidity floor: ${MIN_LIQUIDITY_USD:,}\n")

    print("Fetching active markets per chain...")
    all_markets = fetch_active_markets()
    print(f"\nTotal active markets across all chains: {len(all_markets)}")

    liquid = [m for m in all_markets if m["details"]["liquidity"] >= MIN_LIQUIDITY_USD]
    print(f"Markets with liquidity >= ${MIN_LIQUIDITY_USD:,}: {len(liquid)}")

    liquid.sort(key=lambda m: m["details"]["impliedApy"], reverse=True)

    print(f"\n{'='*100}")
    print(f"TOP {TOP_N_FOR_HISTORY} LIVE-SNAPSHOT CANDIDATES BY IMPLIED (FIXED) APY")
    print(f"{'='*100}")
    header = f"{'Chain':<10}{'Market':<18}{'Expiry':<12}{'DaysLeft':<10}{'ImpliedAPY':<12}{'Liquidity':<14}"
    print(header)
    print("-" * len(header))

    now = datetime.now(timezone.utc)
    top_candidates = liquid[:TOP_N_FOR_HISTORY]
    for m in top_candidates:
        d = m["details"]
        expiry = datetime.fromisoformat(m["expiry"].replace("Z", "+00:00"))
        days_left = (expiry - now).days
        print(
            f"{m['_chain_name']:<10}{m['name']:<18}{m['expiry'][:10]:<12}{days_left:<10}"
            f"{d['impliedApy']*100:>9.1f}%  ${d['liquidity']:>11,.0f}"
        )

    print(f"\n{'='*100}")
    print("SUSTAINED-vs-SPIKE CHECK: 90-day trailing mean impliedApy vs live snapshot")
    print(f"{'='*100}")
    header2 = f"{'Market':<18}{'Live':<10}{'90d Mean':<10}{'30d Mean':<10}{'SpikeRatio':<12}{'HistDays':<10}"
    print(header2)
    print("-" * len(header2))

    results = []
    for m in top_candidates:
        d = m["details"]
        hist = fetch_apy_history(m["_chain_id"], m["address"].split("-")[0] if "-" in m["address"] else m["address"])
        # address field for pt/yt/sy/underlyingAsset is "chainId-0x...", but market address itself is plain 0x...
        addr = m["address"]
        hist = fetch_apy_history(m["_chain_id"], addr)
        live_apy = d["impliedApy"]
        if not hist or len(hist) < 5:
            print(f"{m['name']:<18}{live_apy*100:>7.1f}%   (no usable history)")
            results.append({"market": m, "live": live_apy, "mean90": None, "mean30": None, "hist_days": 0})
            continue
        hist.sort(key=lambda x: x[0])
        last_90 = hist[-90:] if len(hist) >= 90 else hist
        last_30 = hist[-30:] if len(hist) >= 30 else hist
        mean90 = sum(x[2] for x in last_90) / len(last_90)
        mean30 = sum(x[2] for x in last_30) / len(last_30)
        spike_ratio = live_apy / mean90 if mean90 > 0 else float("nan")
        print(
            f"{m['name']:<18}{live_apy*100:>7.1f}%  {mean90*100:>7.1f}%  {mean30*100:>7.1f}%  "
            f"{spike_ratio:>9.2f}x  {len(hist):>8}"
        )
        results.append(
            {"market": m, "live": live_apy, "mean90": mean90, "mean30": mean30, "hist_days": len(hist)}
        )
        time.sleep(0.3)

    print(f"\n{'='*100}")
    print("MONTHLY-EQUIVALENT FIXED YIELD (using 90d mean as the sustainable estimate)")
    print(f"{'='*100}")
    header3 = f"{'Market':<18}{'Underlying':<14}{'Sustained/yr':<14}{'Sustained/mo':<14}{'Clears 5%/mo?':<14}"
    print(header3)
    print("-" * len(header3))
    best = None
    for res in results:
        m = res["market"]
        sustained_annual = res["mean90"] if res["mean90"] is not None else res["live"]
        sustained_monthly = (1 + sustained_annual) ** (1 / 12) - 1
        clears = "YES" if sustained_monthly >= 0.05 else "no"
        print(
            f"{m['name']:<18}{'?':<14}{sustained_annual*100:>10.1f}%   {sustained_monthly*100:>10.2f}%   {clears:<14}"
        )
        if best is None or sustained_monthly > best[1]:
            best = (m["name"], sustained_monthly)

    print(f"\nBest candidate: {best[0]} at {best[1]*100:.2f}%/month (sustained, 90d-mean basis)")
    print("\nNOTE ON RISK (not captured by the APY number above):")
    print("- PT redemption at maturity is in units of the UNDERLYING asset, not USD.")
    print("  If the underlying yield-bearing token de-pegs or its protocol is exploited")
    print("  before maturity, the 'fixed yield' does not protect against that loss --")
    print("  it only fixes the *rate of the underlying asset's own growth*, not its USD value.")
    print("- The highest-impliedApy names in this scan (jrUSDat, STRCx, sUSD3, apyUSD, jrRoyAPYUSD,")
    print("  reUSDe, sUSDat, sENA, etc.) are exotic, newer, lower-TVL structured-yield stablecoin")
    print("  wrappers (leveraged basis / restaking / points-farming vaults), not USDC/USDT --")
    print("  they carry meaningfully higher smart-contract and de-peg risk than a plain stablecoin.")


if __name__ == "__main__":
    main()
