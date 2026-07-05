"""
Thread M (post-A-F follow-up): GMX V2 "GM pool" counterparty-vault yield.

Concept (distinct venue/mechanism from Thread H's JLP, though the same family --
"single/paired-asset counterparty vault that takes the other side of leveraged
perp trades"): GMX V2 GM pools (Arbitrum/Avalanche) hold a long-token + a
short-token (usually the same asset + USDC, sometimes single-asset), earn
trading/borrow/price-impact fees paid by leveraged traders, and are marked to
market against the pool's own token composition -- so, like JLP, the headline
"fee APY" is NOT the total USD return a holder experiences; the pool's value
also moves with an AMM-like impermanent-loss (IL) mechanic as the long token's
price moves relative to entry, on top of net trader-PnL exposure.

This script:
  1. Pulls REAL, live DeFiLlama gmx-v2-perps pool data (fee-only apy + tvlUsd),
     picks the highest-TVL pool per underlying asset (ETH, WBTC.B, SOL, AVAX, LINK).
  2. Pulls each pool's REAL full history (988 daily snapshots back to 2023-10-09)
     to compute live-snapshot vs 7d/30d/90d/180d/full-history mean fee-APY --
     the same spike-vs-sustained check used in iterations 7/19 (funding/Ethena).
  3. Pulls REAL Hyperliquid daily closes for each pool's volatile constituent
     over the same trailing windows, and estimates IL via the standard
     full-range (non-concentrated) constant-product formula
     IL(r) = 2*sqrt(r)/(1+r) - 1, r = end_price/start_price -- disclosed as an
     approximation of the pool's actual GM-token mechanics, not a literal
     backtest of GMX's internal accounting (which also depends on OI-imbalance
     skew, not just price ratio; not obtainable from free public APIs).
  4. Reports net monthly yield = fee-APY/12 (additive) + IL, for each pool over
     each window, against the 5%/month and 10%/month bars.
"""
import requests
from datetime import datetime, timezone

DEFI_LLAMA_POOLS = "https://yields.llama.fi/pools"
DEFI_LLAMA_CHART = "https://yields.llama.fi/chart/{pool}"
HL_INFO = "https://api.hyperliquid.xyz/info"

# underlying asset -> Hyperliquid coin ticker used for IL price-path estimate
ASSET_TO_HL_COIN = {
    "ETH-USDC": "ETH",
    "WBTC.B-USDC": "BTC",
    "SOL-USDC": "SOL",
    "AVAX-USDC": "AVAX",
    "LINK-USDC": "LINK",
}


def fetch_top_pools():
    r = requests.get(DEFI_LLAMA_POOLS, timeout=60)
    r.raise_for_status()
    data = r.json()["data"]
    gmx = [p for p in data if p.get("project") == "gmx-v2-perps"]
    best = {}
    for p in gmx:
        sym = p.get("symbol")
        if sym not in ASSET_TO_HL_COIN:
            continue
        tvl = p.get("tvlUsd") or 0
        if sym not in best or tvl > best[sym]["tvlUsd"]:
            best[sym] = {"tvlUsd": tvl, "apy": p.get("apy"), "pool": p.get("pool"), "chain": p.get("chain")}
    return best


def fetch_pool_history(pool_id):
    r = requests.get(DEFI_LLAMA_CHART.format(pool=pool_id), timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def mean_apy_window(history, days_back):
    if not history:
        return None
    end_dt = datetime.fromisoformat(history[-1]["timestamp"].replace("Z", "+00:00"))
    cutoff = end_dt.timestamp() - days_back * 86400
    vals = [h["apy"] for h in history if datetime.fromisoformat(h["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff and h["apy"] is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def fetch_hl_daily_closes(coin, days=200):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 86400 * 1000
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1d", "startTime": start_ms, "endTime": now_ms},
    }
    r = requests.post(HL_INFO, json=payload, timeout=30)
    r.raise_for_status()
    candles = r.json()
    return [(c["t"], float(c["c"])) for c in candles]


def realized_il(closes, days_back):
    """closes: list of (ts_ms, close). Full-range constant-product IL over trailing days_back."""
    if len(closes) < 2:
        return None
    end_ts, end_price = closes[-1]
    target_ts = end_ts - days_back * 86400 * 1000
    start_ts, start_price = min(closes, key=lambda c: abs(c[0] - target_ts))
    r = end_price / start_price
    il = 2 * (r ** 0.5) / (1 + r) - 1.0
    return il, (r - 1.0)


def main():
    print("Fetching live gmx-v2-perps pool universe from DeFiLlama...")
    top_pools = fetch_top_pools()
    for sym, info in sorted(top_pools.items(), key=lambda kv: -kv[1]["tvlUsd"]):
        print(f"  {sym:12s} chain={info['chain']:10s} tvl=${info['tvlUsd']:,.0f}  live_apy={info['apy']:.2f}%")

    windows = [7, 30, 90, 180]
    print(f"\n{'Pool':12s} {'live':>7s} " + " ".join(f"{w}d_mean".rjust(8) for w in windows) + f" {'full_hist':>9s}  n_days")
    pool_histories = {}
    for sym, info in top_pools.items():
        hist = fetch_pool_history(info["pool"])
        pool_histories[sym] = hist
        live = info["apy"]
        means = [mean_apy_window(hist, w) for w in windows]
        full_mean = sum(h["apy"] for h in hist if h["apy"] is not None) / len([h for h in hist if h["apy"] is not None])
        means_str = " ".join(f"{m:7.2f}%" if m is not None else "    n/a" for m in means)
        print(f"{sym:12s} {live:6.2f}% {means_str} {full_mean:8.2f}%  {len(hist)}")

    print("\nFetching real Hyperliquid daily closes for IL estimate on each underlying asset...")
    coin_closes = {}
    for sym, coin in ASSET_TO_HL_COIN.items():
        if sym not in top_pools:
            continue
        coin_closes[coin] = fetch_hl_daily_closes(coin, days=200)
        print(f"  {coin}: {len(coin_closes[coin])} daily closes fetched")

    print(f"\n{'Pool':12s} {'coin':6s} " + " ".join(f"{w}d_IL".rjust(9) for w in windows))
    net_rows = []
    for sym, info in top_pools.items():
        coin = ASSET_TO_HL_COIN[sym]
        closes = coin_closes[coin]
        ils = [realized_il(closes, w) for w in windows]
        il_str = " ".join(f"{il[0]*100:8.2f}%" if il else "     n/a" for il in ils)
        print(f"{sym:12s} {coin:6s} {il_str}")
        # net monthly yield using 30d fee-apy mean and 30d IL (additive approx)
        fee_apy_30_pct = mean_apy_window(pool_histories[sym], 30)  # e.g. 10.33 means 10.33%/yr
        il_30 = ils[1][0] if ils[1] else None  # fraction, e.g. -0.0075 means -0.75%
        if fee_apy_30_pct is not None and il_30 is not None:
            fee_monthly = (fee_apy_30_pct / 100.0) / 12.0  # fraction/month
            net_monthly = fee_monthly + il_30
            net_rows.append((sym, fee_monthly, il_30, net_monthly))

    print(f"\n{'Pool':12s} {'fee_apy_30d/mo':>15s} {'IL_30d':>9s} {'net_monthly':>12s}  vs_5pct  vs_10pct")
    for sym, fee_monthly, il_30, net_monthly in sorted(net_rows, key=lambda r: -r[3]):
        clears5 = "YES" if net_monthly >= 0.05 else "no"
        clears10 = "YES" if net_monthly >= 0.10 else "no"
        print(f"{sym:12s} {fee_monthly*100:14.2f}% {il_30*100:8.2f}% {net_monthly*100:11.2f}%   {clears5:>6s}  {clears10:>7s}")


if __name__ == "__main__":
    main()
