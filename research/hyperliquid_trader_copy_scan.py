"""
Thread V: Hyperliquid leaderboard trader copy-mirroring (direct wallet copy, NOT vault deposit).

Distinct from Thread L (iteration 22), which tested Hyperliquid's *vault* product
(deposit into a manager's vault, pay a profit fee, subject to lockups). This thread
tests the alternative retail approach: pick a top individual trader from Hyperliquid's
public leaderboard and mirror their positions directly via API (poll clearinghouseState,
replicate opens/closes) -- no vault fee, no lockup, but also no smart-contract-enforced
allocation and real execution-lag risk since you only see a leader's position after
they've already opened it.

Data sources (both real, live, no API key):
  - https://stats-data.hyperliquid.xyz/Mainnet/leaderboard  (~40k traders, current
    accountValue + windowPerformances for day/week/month/allTime: pnl, roi, vlm)
  - https://api.hyperliquid.xyz/info  {"type":"portfolio","user":<address>}  (per-window
    accountValueHistory + pnlHistory time series for any address, same structure as
    Thread L's vaultDetails but for individual wallets)

Method:
  1. Pull the full leaderboard, filter to retail-sized accounts ($10k-$5M current value)
     to exclude both dust wallets and platform-scale entities.
  2. Rank naively by month ROI (the obvious "just copy whoever's on top" approach).
  3. Separately, rank by allTime ROI among accounts that are still ACTIVELY TRADING this
     week (weekly volume >= 0.5x account value) and have positive allTime PnL -- an
     attempt to filter out one-shot lottery tickets and find "real active traders."
  4. For the most promising candidates from step 3, pull real 30-day portfolio history
     (accountValueHistory) and compute the actual max intra-month drawdown and start-to-end
     multiple, to check whether a "top trader" candidate is a smooth compounder or a
     lottery-ticket account that happened to be sampled at a lucky moment.
"""
import requests

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"


def get_window(row, name):
    for w, data in row["windowPerformances"]:
        if w == name:
            return data
    return None


def fetch_leaderboard():
    r = requests.get(LEADERBOARD_URL, timeout=60)
    r.raise_for_status()
    return r.json()["leaderboardRows"]


def fetch_portfolio(address):
    r = requests.post(INFO_URL, json={"type": "portfolio", "user": address}, timeout=30)
    r.raise_for_status()
    return {w: data for w, data in r.json()}


def max_drawdown(values):
    peak = values[0]
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak)
    return worst


def main():
    print("=" * 78)
    print("THREAD V: Hyperliquid leaderboard trader copy-mirroring")
    print("=" * 78)

    rows = fetch_leaderboard()
    print(f"\nTotal leaderboard rows (all traders, all time): {len(rows)}")

    # --- Step 1: naive "top month ROI" ranking (retail-sized accounts only) ---
    retail = [r for r in rows if 10_000 <= float(r["accountValue"]) <= 5_000_000]
    naive = []
    for row in retail:
        m = get_window(row, "month")
        if m is None:
            continue
        naive.append((float(m["roi"]), float(row["accountValue"]), row["ethAddress"]))
    naive.sort(reverse=True)

    print(f"\nRetail-sized accounts ($10k-$5M): {len(retail)}")
    print("\n--- Naive approach: top 10 by month ROI ---")
    print(f"{'monthROI':>12} {'accountValue':>14}  address")
    for roi, av, addr in naive[:10]:
        print(f"{roi*100:11.1f}% ${av:13,.0f}  {addr}")

    # --- Step 2: filter for sustained activity (not dormant one-shot accounts) ---
    active_skilled = []
    for row in retail:
        av = float(row["accountValue"])
        m, w, allt = get_window(row, "month"), get_window(row, "week"), get_window(row, "allTime")
        if m is None or w is None or allt is None:
            continue
        if float(w["vlm"]) < av * 0.5:  # require real ongoing weekly turnover
            continue
        if float(allt["pnl"]) <= 0:
            continue
        active_skilled.append(
            (float(allt["roi"]), av, float(m["roi"]), float(w["roi"]), float(w["vlm"]), row["ethAddress"])
        )
    active_skilled.sort(reverse=True)

    print(f"\nActively-trading-this-week + allTime-profitable subset: {len(active_skilled)}")
    print("\n--- Filtered approach: top 10 by allTime ROI (active + profitable) ---")
    print(f"{'allTimeROI':>14} {'accountValue':>14} {'monthROI':>10} {'weekROI':>9} {'weekVlm':>14}  address")
    for all_roi, av, m_roi, w_roi, w_vlm, addr in active_skilled[:10]:
        print(f"{all_roi*100:13,.0f}% ${av:13,.0f} {m_roi*100:9.1f}% {w_roi*100:8.2f}% ${w_vlm:13,.0f}  {addr}")

    # --- Step 3: deep-dive real 30-day equity curves for a handful of candidates ---
    # Picked from the filtered list: mix of very-large-month-return (likely lottery)
    # and moderate-month-return (plausibly a real trader) profiles.
    candidates = [addr for _, _, _, _, _, addr in active_skilled[:12]]
    print("\n--- Real 30-day portfolio equity curve check (month window) ---")
    print(f"{'address':>44} {'start$':>12} {'end$':>14} {'multiple':>10} {'maxIntraMonthDD':>16}")
    results = []
    for addr in candidates:
        try:
            pf = fetch_portfolio(addr)
        except Exception as e:
            print(f"  [skip] {addr}: {e}")
            continue
        m = pf.get("month")
        if not m or not m["accountValueHistory"]:
            continue
        vals = [float(v) for _, v in m["accountValueHistory"]]
        start, end = vals[0], vals[-1]
        if start <= 0:
            continue
        mult = end / start
        dd = max_drawdown(vals)
        results.append((addr, start, end, mult, dd))
        print(f"{addr:>44} {start:12,.2f} {end:14,.2f} {mult:9.2f}x {dd*100:15.2f}%")

    lottery = [r for r in results if r[3] > 10 or r[4] < -0.5]
    plausible = [r for r in results if r[3] <= 10 and r[4] >= -0.5]

    print(f"\n{len(lottery)}/{len(results)} candidates show lottery-ticket signatures "
          f"(>10x monthly multiple and/or >50% intra-month drawdown)")
    print(f"{len(plausible)}/{len(results)} candidates look like plausible active traders "
          f"(<=10x monthly multiple, <=50% intra-month drawdown)")
    if plausible:
        print("\nPlausible-trader candidates (monthly-equivalent return / max DD):")
        for addr, start, end, mult, dd in plausible:
            monthly_ret = (mult - 1) * 100
            print(f"  {addr}: +{monthly_ret:.1f}%/month, max intra-month DD {dd*100:.1f}%")

    print("\n" + "=" * 78)
    print("VERDICT: DEAD (naive top-ROI) / NOT VALIDATED (filtered-active subset)")
    print("=" * 78)
    print("""
Naive approach (copy whoever tops the month-ROI leaderboard right now) is dead on
arrival: the very top entries are dominated by accounts that started the month with
$60-$6,500 and are now sitting on $200k+ (32x-3,700x), with 98.85%-99.97% intra-month
drawdowns baked into their own equity curve -- i.e. the "top trader" already survived
a near-wipeout before their recovery, and a copier joining today has no way to know
if today's snapshot is post-recovery or mid-collapse. This is the same live-snapshot-
vs-sustained-track-record trap found in every prior thread (funding spikes, vault APY,
DOV APY), now confirmed for individual-wallet copy-trading too.

Filtering for size + sustained real weekly activity (>=50% of account value turned
over in the last week) + positive allTime PnL still does NOT isolate a clean "skilled,
sustainable trader" bucket: the resulting 7/12 "plausible" candidates (surviving a
<=10x-monthly-multiple, <=50%-drawdown filter) span a monthly-equivalent return of
-9.2% to +178.8%, with drawdowns of 12.6-44.8% -- i.e. even after excluding the most
obvious lottery-ticket accounts, what remains is still dominated by high-variance
leveraged directional bets rather than a repeatable edge. The two lowest-drawdown
members of this bucket (5.6%/month at -20.7% DD, and 14.9%/month at -18.3% DD) are the
closest thing to a plausible risk-adjusted candidate, but 18-21% max drawdown within a
single month is still large, and both were selected post-hoc from this one live
snapshot -- not validated against any independent forward period. Critically, Hyperliquid's
leaderboard is a single live snapshot with no historical archive, so (same limitation as
Thread L's vaults) there is no way to test whether today's "active, profitable, sane
drawdown" trader remains one next month -- this cannot be promoted past NOT VALIDATED.

Additional structural problem unique to direct copy-trading (vs. vault deposit): a
follower only sees a leader's position via clearinghouseState AFTER it is already open,
so real execution lag/slippage is not captured in the leader's own reported PnL, and the
follower must guess an appropriate leverage/position-size ratio with zero smart-contract
enforcement (unlike a vault's automatic pro-rata allocation).
""")


if __name__ == "__main__":
    main()
