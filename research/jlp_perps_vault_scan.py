"""
Thread H (post-A-F follow-up): Jupiter Perpetuals (JLP) counterparty-vault yield.

Concept (genuinely distinct mechanism from every prior thread): JLP is a single
token representing a pooled vault that takes the OTHER side of every leveraged
perp trade on Jupiter Perpetuals (Solana). LPs earn ~75% of trading + borrow
fees, but the vault is a real counterparty to traders -- if traders are net
profitable over a period, JLP holders absorb that loss. On top of the P&L vs
traders, JLP's underlying assets (~65% SOL/ETH/BTC, ~35% USDC/USDT per public
composition disclosures) are marked to market, so JLP's USD price also carries
real crypto beta unrelated to any "yield".

This script pulls real historical USD price series (CoinGecko) for JLP itself
and for its three volatile constituents (SOL/BTC/ETH), builds a static-weight
synthetic "hold the basket, no fees" benchmark using JLP's publicly disclosed
target weights, and computes:
  - JLP's real total USD return over 30/90/180 days
  - The benchmark basket's return over the same windows
  - "Excess" (JLP return - naively-weighted benchmark return) as a rough proxy
    for the net fee-vs-trader-PnL yield component, isolated from pure crypto beta

This is a rough decomposition (real weights drift with rebalancing and are not
re-queried daily), but it is sufficient to check whether JLP's advertised
30-60% "APY" figure (which is a fee-yield annualization, not total return)
survives contact with what a real USD-denominated holder actually experienced.
"""
import requests
import time
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CACHE_FILE = DATA_DIR / "jlp_vault_scan_cache.json"

# Publicly disclosed approximate JLP target composition (Jupiter station / multiple
# secondary sources, cross-checked 2026-07-05): ~44% SOL, ~11% BTC, ~9% ETH, ~36% stables (USDC+USDT)
WEIGHTS = {"solana": 0.44, "bitcoin": 0.11, "ethereum": 0.09, "stable": 0.36}

COINGECKO_IDS = {
    "jlp": "jupiter-perpetuals-liquidity-provider-token",
    "solana": "solana",
    "bitcoin": "bitcoin",
    "ethereum": "ethereum",
}


def get_prices(coin_id, days=180, retries=5):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    for attempt in range(retries):
        r = requests.get(url, params={"vs_currency": "usd", "days": days}, timeout=30)
        if r.status_code == 429:
            wait = 15 * (attempt + 1)
            print(f"  rate limited on {coin_id}, waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()["prices"]
    raise RuntimeError(f"failed to fetch {coin_id} after {retries} retries")


def load_or_fetch():
    if CACHE_FILE.exists():
        print(f"Using cached data from {CACHE_FILE}")
        return json.loads(CACHE_FILE.read_text())
    out = {}
    for key, cg_id in COINGECKO_IDS.items():
        print(f"Fetching {key} ({cg_id})...")
        out[key] = get_prices(cg_id, days=180)
        time.sleep(20)  # CoinGecko free tier is aggressively rate-limited
    CACHE_FILE.write_text(json.dumps(out))
    return out


def series_return(prices, days_back):
    """prices: list of [ts_ms, price]. Returns pct return over the trailing `days_back` window."""
    if not prices:
        return None
    end_price = prices[-1][1]
    end_ts = prices[-1][0]
    target_ts = end_ts - days_back * 86400 * 1000
    # find closest sample to target_ts
    start = min(prices, key=lambda p: abs(p[0] - target_ts))
    return (end_price / start[1]) - 1.0


def main():
    data = load_or_fetch()
    jlp, sol, btc, eth = data["jlp"], data["solana"], data["bitcoin"], data["ethereum"]

    print(f"\nData points: JLP={len(jlp)}  SOL={len(sol)}  BTC={len(btc)}  ETH={len(eth)}")
    print(f"JLP window: {jlp[0][0]/1000:.0f} -> {jlp[-1][0]/1000:.0f} (unix)")

    windows = [30, 90, 180]
    print(f"\n{'Window':>8} {'JLP ret':>10} {'SOL ret':>10} {'BTC ret':>10} {'ETH ret':>10} {'Basket ret':>12} {'Excess (JLP-basket)':>20}")
    for w in windows:
        jlp_r = series_return(jlp, w)
        sol_r = series_return(sol, w)
        btc_r = series_return(btc, w)
        eth_r = series_return(eth, w)
        basket_r = (
            WEIGHTS["solana"] * sol_r
            + WEIGHTS["bitcoin"] * btc_r
            + WEIGHTS["ethereum"] * eth_r
            + WEIGHTS["stable"] * 0.0  # stables assumed flat in USD
        )
        excess = jlp_r - basket_r
        print(f"{w:>7}d {jlp_r*100:>9.2f}% {sol_r*100:>9.2f}% {btc_r*100:>9.2f}% {eth_r*100:>9.2f}% {basket_r*100:>11.2f}% {excess*100:>19.2f}%")

    # Monthly-equivalent figures for the two decompositions
    jlp_180 = series_return(jlp, 180)
    basket_180 = (
        WEIGHTS["solana"] * series_return(sol, 180)
        + WEIGHTS["bitcoin"] * series_return(btc, 180)
        + WEIGHTS["ethereum"] * series_return(eth, 180)
    )
    excess_180 = jlp_180 - basket_180
    monthly_total = (1 + jlp_180) ** (30 / 180) - 1
    monthly_excess = excess_180 / 6.0  # additive approximation over 6 months

    print(f"\nJLP total USD return, 180d: {jlp_180*100:.2f}%  (~{monthly_total*100:.2f}%/month compounded, TOTAL return incl. beta)")
    print(f"Static-weight-basket return, 180d: {basket_180*100:.2f}%")
    print(f"Excess ('yield' net of beta), 180d: {excess_180*100:.2f}%  (~{monthly_excess*100:.2f}%/month additive)")


if __name__ == "__main__":
    main()
