"""
Crypto cash-and-carry (funding-basis) backtest — the LEGAL, market-neutral analog
of the Polymarket reward-farming idea.

Trade: long spot (or buy-and-hold the coin), short the same-size perpetual future.
Price exposure cancels (delta ~0). You COLLECT the perp funding rate when it's
positive (longs pay shorts), PAY it when negative. The question this answers with
real history (not a snapshot): what is the realized, net-of-fees annualized yield,
how often is funding actually positive, and how bad are the negative stretches?

No forecasting edge required — this is yield capture, not prediction. That's why
the coin-flip-model problem that killed the Elon thread does not apply here.

Data: Binance USD-M public funding history (/fapi/v1/fundingRate, 8h cadence).
Read-only, no auth, no geo gate. Trading crypto is legal/regulated (MiCA) in DE;
the basis can be run on a CEX or via on-chain perps (Hyperliquid/dYdX/GMX).

    python -m research.crypto_basis_backtest
    python -m research.crypto_basis_backtest --symbols BTCUSDT ETHUSDT SOLUSDT --roundtrip-fee 0.0008
Outputs: console table + data/edge_latency/crypto_basis.csv
"""
from __future__ import annotations

import argparse
import csv
import statistics as st
import time
from pathlib import Path

import requests

OUT = Path("data/edge_latency")
FUND = "https://fapi.binance.com/fapi/v1/fundingRate"
DEFAULT = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
           "LINKUSDT", "AVAXUSDT", "LTCUSDT", "ARBUSDT"]


def fetch_funding(S: requests.Session, symbol: str, pages: int) -> list[tuple[int, float]]:
    """Most-recent funding prints, paged backwards. Returns [(ms, rate), ...] oldest-first."""
    out, end = [], None
    for _ in range(pages):
        params = {"symbol": symbol, "limit": 1000}
        if end is not None:
            params["endTime"] = end
        try:
            j = S.get(FUND, params=params, timeout=20).json()
        except Exception:
            break
        if not isinstance(j, list) or not j:
            break
        for x in j:
            try:
                out.append((int(x["fundingTime"]), float(x["fundingRate"])))
            except Exception:
                pass
        end = j[0]["fundingTime"] - 1   # page older
        if len(j) < 1000:
            break
        time.sleep(0.15)
    out = sorted(set(out))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="crypto funding-basis (cash-and-carry) backtest")
    p.add_argument("--symbols", nargs="+", default=DEFAULT)
    p.add_argument("--pages", type=int, default=1, help="x1000 funding prints (~333 days each)")
    p.add_argument("--roundtrip-fee", type=float, default=0.0008,
                   help="total taker fee both legs in+out as a fraction (0.0008 = 8bps); amortized over the hold")
    p.add_argument("--hold-days", type=float, default=30.0, help="assumed hold to amortize entry/exit fee")
    a = p.parse_args(argv)
    S = requests.Session(); S.headers.update({"User-Agent": "Mozilla/5.0"})

    rows = []
    for sym in a.symbols:
        h = fetch_funding(S, sym, a.pages)
        if len(h) < 30:
            continue
        rates = [r for _, r in h]
        days = (h[-1][0] - h[0][0]) / 86400_000.0
        periods_per_yr = len(rates) / days * 365 if days > 0 else 0
        mean_r = st.mean(rates)
        gross_apr = mean_r * (len(rates) / days) * 365 * 100 if days > 0 else 0
        # fee drag: one round trip (in+out, both legs) amortized over the hold window
        fee_apr = a.roundtrip_fee / a.hold_days * 365 * 100
        net_apr = gross_apr - fee_apr
        pos_frac = sum(1 for r in rates if r > 0) / len(rates)
        # worst sustained negative stretch (consecutive funding you'd PAY), annualized
        worst, run = 0.0, 0.0
        for r in rates:
            run = min(0.0, run + r) if r < 0 else 0.0
            worst = min(worst, run)
        worst_apr = worst * periods_per_yr * 100 / len(rates) * len(rates)  # cumulative %, see note
        rows.append({
            "symbol": sym, "days": round(days, 0), "n": len(rates),
            "gross_apr": round(gross_apr, 1), "net_apr": round(net_apr, 1),
            "pos_frac": round(pos_frac, 3),
            "worst_neg_cum_pct": round(worst * 100, 2),
            "vol_apr": round(st.pstdev(rates) * periods_per_yr ** 0.5 * 100, 1) if len(rates) > 1 else 0,
        })

    rows.sort(key=lambda r: -r["net_apr"])
    print(f"\nCash-and-carry funding yield (long spot / short perp), Binance history, "
          f"fee {a.roundtrip_fee*1e4:.0f}bps over {a.hold_days:.0f}d hold\n")
    hdr = f"{'symbol':<9} {'days':>5} {'grossAPR':>8} {'netAPR':>7} {'pos%':>5} {'worstNeg%':>9} {'volAPR':>7}"
    print(hdr); print("-" * len(hdr))
    for r in rows:
        print(f"{r['symbol']:<9} {r['days']:>5.0f} {r['gross_apr']:>7.1f}% {r['net_apr']:>6.1f}% "
              f"{r['pos_frac']*100:>4.0f}% {r['worst_neg_cum_pct']:>8.2f}% {r['vol_apr']:>6.1f}%")

    if rows:
        majors = [r for r in rows if r["symbol"] in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")]
        pool = majors or rows
        med = st.median([r["net_apr"] for r in pool])
        print(f"\n=== VERDICT ===")
        print(f"  median NET APR (majors): {med:.1f}%  — market-neutral, no forecasting edge")
        print(f"  funding positive ~{st.mean([r['pos_frac'] for r in pool])*100:.0f}% of periods on majors")
        print(f"  REAL but modest: this is a yield (~capital * {med:.0f}%/yr), not a jackpot.")
        print(f"  CATCHES: funding flips negative (worstNeg% = drawdown you'd pay through);")
        print(f"           needs capital on 2 venues; EU retail CEX-perp leverage is ESMA-")
        print(f"           restricted, so execution is likely on-chain perps (HL/dYdX/GMX) =")
        print(f"           smart-contract + liquidation + tax-reporting risk. Not free money.")
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT / "crypto_basis.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        print(f"\n  saved {OUT/'crypto_basis.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
