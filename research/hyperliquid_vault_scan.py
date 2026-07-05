"""
Thread L: Hyperliquid public-vault copy-deposit scan.

A new mechanism family (manager/agency risk via passive copy-deposit into a
Hyperliquid-native vault) beyond every prior thread in this objective, which
have all been either market-neutral yield-harvesting, single-asset directional
momentum, or cross-asset pairs mean-reversion run by OUR OWN code. This thread
instead asks: is depositing into an existing, already-profitable third-party
Hyperliquid vault (HLP itself, or a "protocol" vault run by another trader) a
higher-yield, still-automatable alternative? Deposits/withdrawals are a single
signed on-chain action and can be scripted; the "strategy" itself is opaque and
run by someone else, which is the core new risk this thread quantifies.

Data sources (both real, live, no API key):
  - https://stats-data.hyperliquid.xyz/Mainnet/vaults  (full universe snapshot,
    ~9,463 vaults ever created, includes closed vaults -> lets us measure
    platform-wide survivorship/blowup base rates)
  - https://api.hyperliquid.xyz/info {"type": "vaultDetails", ...} (per-vault
    real daily-resampled account-value + cumulative-PnL history since
    inception, for any vault address)

Method:
  1. Pull the full vault universe; report what fraction of ALL vaults ever
     created are already closed, and what fraction of those closed with a
     negative all-time cumulative PnL (survivorship/blowup base rate).
  2. Filter to OPEN vaults with TVL >= $500k (large enough to not be a dust/
     wash-trading shell) and pull each one's real vaultDetails.
  3. For each candidate, compute:
       - live "apr" field (Hyperliquid's own recent/annualized snapshot number)
       - last-30-day return: cumPnl(month) / equity at start of month
       - "sustained" all-time monthly return: allTime cumPnl / avg all-time
         equity / (allTime days / 30) -- a flow-blind, PnL-only measure so
         deposit/withdrawal noise in the raw TVL curve doesn't contaminate it
       - max PnL-based drawdown, expressed as % of the vault's contemporaneous
         equity (not % of peak PnL dollars), so a $2M drawdown on a $3M vault
         reads as ~-67%, comparable to an equity-curve drawdown
  4. Cross-check the live "apr" against the sustained-monthly figure for each
     candidate -- if live snapshot >> sustained history, flag it as the same
     spike-vs-mean trap already found for funding rates (iteration 7) and
     vault-token "real yield" (iterations 18/19).
"""
import json
import time
from datetime import datetime, timezone

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
STATS_VAULTS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
MIN_TVL = 500_000


def fmt_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def get_vault_details(addr):
    r = requests.post(INFO_URL, json={"type": "vaultDetails", "vaultAddress": addr}, timeout=20)
    r.raise_for_status()
    return r.json()


def survivorship_base_rate(universe):
    closed = [v for v in universe if v["summary"].get("isClosed")]

    def alltime_final_pnl(v):
        for k, series in v.get("pnls", []):
            if k == "allTime":
                vals = [float(x) for x in series]
                return vals[-1] if vals else None
        return None

    neg = pos = flat = 0
    for v in closed:
        pnl = alltime_final_pnl(v)
        if pnl is None:
            continue
        if pnl < -1:
            neg += 1
        elif pnl > 1:
            pos += 1
        else:
            flat += 1
    return {
        "total_ever_created": len(universe),
        "closed": len(closed),
        "closed_pct": len(closed) / len(universe) * 100,
        "closed_negative_pnl": neg,
        "closed_negative_pct_of_closed": neg / len(closed) * 100,
        "closed_positive_pnl": pos,
        "closed_flat_dust": flat,
    }


def analyze_vault(summary, details):
    portfolio = dict(details.get("portfolio", []))
    if "month" not in portfolio or "allTime" not in portfolio:
        return None
    m_av = portfolio["month"]["accountValueHistory"]
    m_pnl = portfolio["month"]["pnlHistory"]
    a_av = portfolio["allTime"]["accountValueHistory"]
    a_pnl = portfolio["allTime"]["pnlHistory"]
    if not m_av or not a_av or not a_pnl:
        return None

    month_start_eq = float(m_av[0][1]) or 1.0
    month_cumpnl = float(m_pnl[-1][1]) - float(m_pnl[0][1])
    month_ret_pct = month_cumpnl / month_start_eq * 100

    a_av_map = {ts: float(v) for ts, v in a_av}
    days_total = (a_av[-1][0] - a_av[0][0]) / 86_400_000
    avg_eq = sum(a_av_map.values()) / len(a_av_map) if a_av_map else 1.0
    alltime_cumpnl = float(a_pnl[-1][1]) - float(a_pnl[0][1])
    sustained_monthly_pct = (
        (alltime_cumpnl / avg_eq) / (days_total / 30) * 100 if days_total > 0 else 0.0
    )

    peak = float("-inf")
    max_dd_pct = 0.0
    for ts, val in a_pnl:
        val = float(val)
        peak = max(peak, val)
        eq = a_av_map.get(ts, 0) or 1.0
        dd_pct = (val - peak) / eq * 100
        max_dd_pct = min(max_dd_pct, dd_pct)

    return {
        "name": summary["name"],
        "addr": summary["vaultAddress"],
        "tvl": float(summary.get("tvl", 0)),
        "live_apr_pct": details.get("apr", 0) * 100,
        "month_ret_pct": month_ret_pct,
        "alltime_days": days_total,
        "alltime_sustained_monthly_pct": sustained_monthly_pct,
        "max_pnl_drawdown_pct_of_eq": max_dd_pct,
        "leader_commission_pct": (details.get("leaderCommission") or 0) * 100,
        "leader_fraction_pct": (details.get("leaderFraction") or 0) * 100,
        "followers": len(details.get("followers", [])),
    }


def main():
    print("Pulling full Hyperliquid vault universe...")
    universe = requests.get(STATS_VAULTS_URL, timeout=30).json()
    base_rate = survivorship_base_rate(universe)
    print(json.dumps(base_rate, indent=2))

    def tvl(v):
        try:
            return float(v["summary"].get("tvl", 0))
        except (TypeError, ValueError):
            return 0.0

    open_vaults = [v for v in universe if not v["summary"].get("isClosed") and tvl(v) >= MIN_TVL]
    open_vaults.sort(key=tvl, reverse=True)
    print(f"\n{len(open_vaults)} open vaults with TVL >= ${MIN_TVL:,}")

    results = []
    for v in open_vaults:
        s = v["summary"]
        try:
            details = get_vault_details(s["vaultAddress"])
        except requests.RequestException as e:
            print("  fetch error", s["vaultAddress"], e)
            continue
        row = analyze_vault(s, details)
        if row:
            results.append(row)
        time.sleep(0.15)

    results.sort(key=lambda x: x["alltime_sustained_monthly_pct"], reverse=True)
    print(
        f"\n{'name':32s} {'tvl':>12s} {'liveAPR%':>9s} {'month%':>8s} {'days':>6s} "
        f"{'sustMo%':>8s} {'maxDD%':>8s} {'lFrac%':>7s} {'flw':>4s}"
    )
    for x in results:
        name = x["name"].encode("ascii", "replace").decode()[:32]
        print(
            f"{name:32s} {x['tvl']:>12,.0f} {x['live_apr_pct']:>9.1f} {x['month_ret_pct']:>8.1f} "
            f"{x['alltime_days']:>6.0f} {x['alltime_sustained_monthly_pct']:>8.1f} "
            f"{x['max_pnl_drawdown_pct_of_eq']:>8.1f} {x['leader_fraction_pct']:>6.1f}% {x['followers']:>4d}"
        )

    json.dump(
        {"survivorship_base_rate": base_rate, "vaults": results},
        open("data/hyperliquid_vault_scan_results.json", "w"),
        indent=2,
    )
    print("\nSaved data/hyperliquid_vault_scan_results.json")


if __name__ == "__main__":
    main()
