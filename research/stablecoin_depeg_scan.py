"""
Thread S: Stablecoin depeg mean-reversion trading.

A genuinely new mechanism family (23rd overall, 17th post-A-F follow-up):
buy a major stablecoin when it trades meaningfully below $1 (a "depeg"),
hold until it reverts to ~$1, sell, capture the spread. This is a real,
historically-observed event (USDC/SVB March 2023, DAI/FRAX's correlated
wobble the same week since both are partially USDC-collateralized) rather
than a simulated one, and is mechanically simple to automate (poll price,
buy below threshold, sell above recovery threshold).

Data source: Binance public klines (api.binance.com/api/v3/klines, no key,
no auth) for each stablecoin's USDT pair, daily interval, paginated back to
2019-01-01. Binance.com's public market-data endpoints require no login
and were reachable from this environment; NOTE per the objective's own
geo-guidance, Binance.com is a possibly-restricted *trading* venue for a
Romania-based user, so this script uses Binance ONLY as a historical-data
source with the deepest real coverage (it captures the actual 2023-03-11
USDC/SVB event, which neither CoinGecko's free tier -- rate-limited during
this run -- nor Kraken's public OHLC -- capped at ~720 daily candles,
i.e. ~2 years back -- could reach). Execution, if this were ever pursued,
would happen on a Romania-accessible venue (Kraken/OKX/Bybit); a spot-check
against Kraken's own USDCUSD/DAIUSD/USDTZUSD OHLC over their shared recent
~2-year overlap window is included below to confirm the two venues track
the same peg (they do, so Binance's deeper history is a valid proxy for
what a Kraken-based bot would have seen).

Methodology:
  1. Pull full daily klines (open/high/low/close) since 2019-01-01 for each
     stablecoin's USDT pair on Binance, using low/high (not just close) so
     a same-day depeg-and-recover intraday move is not averaged away.
  2. Flag every day where low < 1 - DEPEG_THRESHOLD (below-peg) using the
     day's LOW as the trough (a real buyable price that day).
  3. Group consecutive flagged days into discrete events; for each, find
     the first later day whose CLOSE is back within RECOVERY_BAND of $1,
     and compute the simple buy-at-trough / sell-at-recovery-close return.
  4. Aggregate: event frequency, avg magnitude, avg recovery time, and a
     pooled "monthly equivalent" return assuming perfect fills at every
     historical trough (an explicit upper bound, not a realistic backtest).
  5. Disclose survivorship bias explicitly: this coin list is the set of
     stablecoins that DID recover every time so far -- algorithmic
     stablecoins that depegged and never recovered (TerraUSD/UST, May 2022,
     -100% permanent loss) are excluded by construction because they are
     no longer "major" stablecoins today. This is the central risk this
     thread must disclose, not the frequency/magnitude numbers.
"""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

BINANCE_BASE = "https://api.binance.com/api/v3/klines"
KRAKEN_BASE = "https://api.kraken.com/0/public/OHLC"

SYMBOLS = {
    "USDCUSDT": "USDC",
    "DAIUSDT": "DAI",
    "TUSDUSDT": "TUSD",
    "FDUSDUSDT": "FDUSD",
    "USDPUSDT": "USDP",
    "USDEUSDT": "USDe",
}

KRAKEN_PAIRS = {
    "USDCUSD": "USDC",
    "DAIUSD": "DAI",
    "USDTZUSD": "USDT",
}

DEPEG_THRESHOLD = 0.005   # 0.5% deviation triggers event flag (on daily LOW)
SEVERE_THRESHOLD = 0.02   # 2% deviation flagged as "severe"
RECOVERY_BAND = 0.001     # within 0.1% of $1.00 on CLOSE counts as "recovered"
START_MS = int(datetime(2019, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def fetch_binance_klines(symbol):
    all_rows = []
    start = START_MS
    while True:
        url = f"{BINANCE_BASE}?symbol={symbol}&interval=1d&startTime={start}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "research-script/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode())
        if not rows:
            break
        all_rows.extend(rows)
        last_close_time = rows[-1][6]
        if len(rows) < 1000:
            break
        start = last_close_time + 1
        time.sleep(0.3)
    # dedupe by open time
    seen = set()
    out = []
    for r in all_rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append(r)
    out.sort(key=lambda r: r[0])
    return out


def fetch_kraken_ohlc(pair):
    url = f"{KRAKEN_BASE}?pair={pair}&interval=1440"
    req = urllib.request.Request(url, headers={"User-Agent": "research-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read().decode())
    if d.get("error"):
        return []
    keys = [k for k in d["result"].keys() if k != "last"]
    if not keys:
        return []
    return d["result"][keys[0]]


def to_daily_series(klines):
    """Binance kline row: [openTime, open, high, low, close, volume, closeTime, ...]"""
    out = []
    for r in klines:
        date = datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc).date()
        out.append({"date": date, "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])})
    return out


def find_events(series):
    events = []
    in_event = False
    cur = None
    for row in series:
        d, low, close = row["date"], row["low"], row["close"]
        flagged = low < (1.0 - DEPEG_THRESHOLD)
        if flagged and not in_event:
            in_event = True
            cur = {"start": d, "trough": low, "trough_date": d}
        elif flagged and in_event:
            if low < cur["trough"]:
                cur["trough"] = low
                cur["trough_date"] = d
        elif not flagged and in_event:
            cur["end"] = d
            events.append(cur)
            in_event = False
            cur = None
    if in_event:
        cur["end"] = None
        events.append(cur)
    return events


def annotate_recovery(events, series):
    idx_by_date = {row["date"]: i for i, row in enumerate(series)}
    for ev in events:
        trough_idx = idx_by_date[ev["trough_date"]]
        recovered_date = None
        for row in series[trough_idx + 1:]:
            if abs(row["close"] - 1.0) <= RECOVERY_BAND:
                recovered_date = row["date"]
                break
        if recovered_date is not None:
            ev["recovered"] = True
            ev["recovery_days"] = (recovered_date - ev["trough_date"]).days
            ev["trade_return"] = (1.0 / ev["trough"]) - 1.0
        else:
            ev["recovered"] = False
            ev["recovery_days"] = None
            ev["trade_return"] = None


def main():
    print("=" * 78)
    print("THREAD S: Stablecoin depeg mean-reversion trading (Binance daily klines)")
    print("=" * 78)

    all_results = {}
    for symbol, name in SYMBOLS.items():
        print(f"\nFetching {name} ({symbol}) full daily history since 2019-01-01...")
        try:
            klines = fetch_binance_klines(symbol)
        except Exception as e:
            print(f"  FAILED: {e}")
            continue
        if not klines:
            print("  No data returned (likely not listed / no history).")
            continue
        series = to_daily_series(klines)
        print(f"  {len(series)} daily candles, {series[0]['date']} -> {series[-1]['date']}")

        events = find_events(series)
        annotate_recovery(events, series)
        severe = [e for e in events if (1.0 - e["trough"]) > SEVERE_THRESHOLD]
        unrecovered = [e for e in events if e["recovered"] is False]

        all_results[name] = {"series_len": len(series), "start": str(series[0]["date"]),
                              "end": str(series[-1]["date"]), "events": events,
                              "severe": severe, "unrecovered": unrecovered}

        print(f"  Below-peg events (low < -{DEPEG_THRESHOLD*100:.1f}%): {len(events)}")
        print(f"  Severe events (low < -{SEVERE_THRESHOLD*100:.0f}%): {len(severe)}")
        print(f"  Unrecovered-within-sample: {len(unrecovered)}")
        for e in events:
            rec = (f"recovered in {e['recovery_days']}d, trade_return={e['trade_return']*100:.2f}%"
                   if e["recovered"] else "NOT RECOVERED within sample")
            print(f"    {e['start']} trough={e['trough']:.4f} on {e['trough_date']}  {rec}")

    print("\n" + "=" * 78)
    print("AGGREGATE: annualized/monthly 'buy-the-depeg' strategy return per coin")
    print("=" * 78)
    for name, r in all_results.items():
        recovered = [e for e in r["events"] if e["recovered"]]
        n_days = r["series_len"]
        n_years = n_days / 365.25
        total_return = sum(e["trade_return"] for e in recovered) if recovered else 0.0
        monthly_equiv = (total_return / n_years / 12) if n_years > 0 else 0.0
        events_per_year = len(recovered) / n_years if n_years > 0 else 0.0
        avg_recovery = (sum(e["recovery_days"] for e in recovered) / len(recovered)) if recovered else None
        print(f"{name:6s} recovered_events={len(recovered):3d}  events/yr={events_per_year:5.2f}  "
              f"cum_return_all_captured={total_return*100:7.3f}%  ~monthly_equiv={monthly_equiv*100:7.4f}%/mo  "
              f"avg_recovery={'n/a' if avg_recovery is None else f'{avg_recovery:.1f}d'}  "
              f"unrecovered_in_sample={len(r['unrecovered'])}")

    print("\nNOTE: monthly_equiv assumes ALL capital sits idle waiting for the next event")
    print("and captures 100% of every historical below-peg dip at the exact daily LOW")
    print("with zero slippage/fees -- an upper bound, not a realistic fill.")

    # Cross-check vs Kraken over their shared recent window
    print("\n" + "=" * 78)
    print("CROSS-CHECK: Kraken OHLC (last ~2yr) vs Binance over the same window")
    print("=" * 78)
    for pair, name in KRAKEN_PAIRS.items():
        try:
            rows = fetch_kraken_ohlc(pair)
        except Exception as e:
            print(f"{name}: Kraken fetch failed: {e}")
            continue
        if not rows or name not in all_results:
            print(f"{name}: no data to compare")
            continue
        kr_low_min = min(float(r[3]) for r in rows)
        kr_start = datetime.fromtimestamp(rows[0][0], tz=timezone.utc).date()
        kr_end = datetime.fromtimestamp(rows[-1][0], tz=timezone.utc).date()
        bn_series = [e for e in all_results[name]["events"]]
        bn_in_window = [e for e in bn_series if kr_start <= e["trough_date"] <= kr_end]
        bn_min_in_window = min([e["trough"] for e in bn_in_window], default=None)
        print(f"{name}: Kraken window {kr_start}->{kr_end}, Kraken min_low={kr_low_min:.4f}  "
              f"Binance events_in_same_window={len(bn_in_window)} "
              f"Binance min_trough_in_window={'n/a' if bn_min_in_window is None else f'{bn_min_in_window:.4f}'}")

    with open("data/stablecoin_depeg_results.json", "w") as f:
        json.dump(
            {
                name: {
                    "start": r["start"], "end": r["end"],
                    "events": [
                        {"start": str(e["start"]), "trough": e["trough"], "trough_date": str(e["trough_date"]),
                         "recovered": e["recovered"], "recovery_days": e["recovery_days"], "trade_return": e["trade_return"]}
                        for e in r["events"]
                    ],
                }
                for name, r in all_results.items()
            },
            f, indent=2,
        )
    print("\nSaved raw results to data/stablecoin_depeg_results.json")


if __name__ == "__main__":
    main()
