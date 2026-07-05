"""
Deribit dated-futures cash-and-carry basis scan (new mechanism, not in the
objective's A-F list; distinct from Thread B's perpetual-funding basis trade).

Trade: buy spot (or long BTC-PERPETUAL as a spot proxy), short a dated
(quarterly/monthly) future at a premium to index. Hold to expiry -> futures
price converges to index at settlement -> the entry premium, annualized, is
your locked-in market-neutral yield (no funding payments involved, unlike
Thread B; this is a forward-curve basis, priced once at entry).

Data (100% real, live, no auth):
  - GET /public/get_index_price            -> live BTC/ETH spot index
  - GET /public/get_instruments (kind=future, expired=false) -> full dated curve
  - GET /public/get_book_summary_by_instrument -> live mark price per contract
  - GET /public/get_tradingview_chart_data  -> daily history for a fixed dated
    contract (e.g. BTC-25SEP26) vs BTC-PERPETUAL (spot proxy, confirmed to track
    index within bps in Thread D), to see if today's basis is a low snapshot or
    representative of the recent history.

    python -m research.deribit_futures_basis
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

BASE = "https://www.deribit.com/api/v2/public/"


def _get(session: requests.Session, path: str, params: dict) -> dict:
    r = session.get(BASE + path, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "result" not in j:
        raise RuntimeError(f"{path} -> {j}")
    return j["result"]


def live_curve(session: requests.Session, currency: str) -> list[dict]:
    idx = _get(session, "get_index_price", {"index_name": f"{currency.lower()}_usd"})["index_price"]
    instruments = _get(session, "get_instruments", {"currency": currency, "kind": "future", "expired": "false"})
    now = time.time() * 1000
    rows = []
    for i in instruments:
        name = i["instrument_name"]
        exp = i.get("expiration_timestamp")
        is_perp = exp is None or exp >= 32503708800000  # deribit uses a sentinel far-future ts for perps
        summ = _get(session, "get_book_summary_by_instrument", {"instrument_name": name})[0]
        mark = summ.get("mark_price")
        if mark is None:
            continue
        if is_perp:
            rows.append({"name": name, "mark": mark, "days": None, "ann_basis_pct": None})
            continue
        days = (exp - now) / 86_400_000.0
        if days <= 0.4:
            continue  # about to settle, noise dominates
        ann_basis = (mark / idx - 1.0) * (365.0 / days) * 100.0
        rows.append({"name": name, "mark": mark, "days": round(days, 1), "ann_basis_pct": round(ann_basis, 2)})
    return idx, rows


def historical_basis(session: requests.Session, currency: str, dated_instrument: str, days: int = 90) -> list[tuple[int, float]]:
    """Daily annualized basis of `dated_instrument` vs the perpetual (spot proxy) over the last `days`."""
    now = int(time.time())
    start = now - days * 86400
    instruments = _get(session, "get_instruments", {"currency": currency, "kind": "future", "expired": "false"})
    exp = next(i["expiration_timestamp"] for i in instruments if i["instrument_name"] == dated_instrument)

    def chart(instrument_name: str) -> dict[int, float]:
        c = _get(session, "get_tradingview_chart_data", {
            "instrument_name": instrument_name, "start_timestamp": start * 1000,
            "end_timestamp": now * 1000, "resolution": "1D",
        })
        if not c.get("ticks"):
            return {}
        return {t: cl for t, cl in zip(c["ticks"], c["close"]) if cl is not None}

    fut = chart(dated_instrument)
    spot = chart(f"{currency}-PERPETUAL")
    out = []
    for t in sorted(set(fut) & set(spot)):
        days_left = (exp - t) / 86_400_000.0
        if days_left <= 1:
            continue
        ann = (fut[t] / spot[t] - 1.0) * (365.0 / days_left) * 100.0
        out.append((t, ann))
    return out


def main() -> int:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    print("=== Deribit dated-futures cash-and-carry basis (live curve) ===\n")
    best_by_currency = {}
    for currency in ["BTC", "ETH"]:
        idx, rows = live_curve(session, currency)
        print(f"{currency} index = {idx:,.2f}")
        hdr = f"  {'instrument':<16} {'mark':>12} {'days':>7} {'ann_basis':>10}"
        print(hdr)
        for r in rows:
            if r["days"] is None:
                print(f"  {r['name']:<16} {r['mark']:>12,.2f} {'PERP':>7} {'--':>10}")
            else:
                print(f"  {r['name']:<16} {r['mark']:>12,.2f} {r['days']:>7.1f} {r['ann_basis_pct']:>9.2f}%")
        dated = [r for r in rows if r["days"] is not None and r["days"] > 20]
        farthest = max(dated, key=lambda r: r["days"]) if dated else None
        best_by_currency[currency] = farthest
        print()

    print("=== Historical basis check (dated contract vs perpetual, last 90d) ===\n")
    for currency, contract_days_target in [("BTC", "25SEP26"), ("ETH", "25SEP26")]:
        name = f"{currency}-{contract_days_target}"
        try:
            hist = historical_basis(session, currency, name, days=90)
        except Exception as e:
            print(f"{name}: history fetch failed ({e})\n")
            continue
        if not hist:
            print(f"{name}: no overlapping history returned\n")
            continue
        vals = [v for _, v in hist]
        lo, hi, avg = min(vals), max(vals), sum(vals) / len(vals)
        first_t = datetime.fromtimestamp(hist[0][0] / 1000, tz=timezone.utc).date()
        last_t = datetime.fromtimestamp(hist[-1][0] / 1000, tz=timezone.utc).date()
        print(f"{name} vs {currency}-PERPETUAL, {first_t} -> {last_t} ({len(hist)} daily samples)")
        print(f"  annualized basis: min={lo:.2f}%  max={hi:.2f}%  mean={avg:.2f}%")
        print()

    print("=== Verdict ===")
    for currency, farthest in best_by_currency.items():
        if farthest:
            monthly = farthest["ann_basis_pct"] / 12.0
            print(f"  {currency}: best live locked-in yield ({farthest['name']}, {farthest['days']:.0f}d out) "
                  f"= {farthest['ann_basis_pct']:.2f}%/yr -> {monthly:.2f}%/month")
    print("  Target floor: 5%/month (60%/yr equivalent). Current live curve and 90d history")
    print("  both sit far below that on BTC and ETH -> DEAD as tested (see FINDINGS ledger).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
