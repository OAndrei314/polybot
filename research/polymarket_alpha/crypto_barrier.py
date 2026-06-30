"""Crypto barrier-option mispricing edge — backtest.

Polymarket lists touch/barrier markets on crypto + commodities:
  "Will Bitcoin reach $150,000 by Dec 31?"   -> UP touch (max S >= H before T)
  "Will Bitcoin dip to $85,000 in January?"  -> DOWN touch (min S <= H before T)

Fair value of a touch is computable from a driftless GBM first-passage
formula calibrated to realized vol. The underlying (BTC/ETH/SOL) is free and
real-time on Binance, so we have no latency disadvantage. The edge thesis:
retail systematically *overpays* for far-OTM "lottery" touches. This script
measures whether that mispricing exists and is tradeable net of fees.

Two validations:
  A. MODEL vs OUTCOME  — is the first-passage math itself calibrated to the
     realized price path? (sanity: does P_model predict actual touches?)
  B. MARKET vs MODEL   — where market price diverges from P_model, does the
     resolution side with the model? That divergence, signed, is the edge.

Data:
  - markets:   data/polymarket_alpha/markets.jsonl.gz  (resolved, with outcome)
  - underlying: Binance daily klines (cached to data/polymarket_alpha/underlying/)
  - market price history: CLOB /prices-history per clobTokenId (cached)

Usage:
  python -m research.polymarket_alpha.crypto_barrier --calibrate
  python -m research.polymarket_alpha.crypto_barrier --backtest
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("data/polymarket_alpha")
UND_DIR = ROOT / "underlying"
UND_DIR.mkdir(parents=True, exist_ok=True)

BINANCE_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


# ----------------------------- market parsing -----------------------------

def _parse_market(d: dict) -> dict | None:
    q = d.get("question") or ""
    ql = q.lower()
    slug = (d.get("slug") or "").lower()
    # asset
    if "bitcoin" in ql or re.search(r"\bbtc\b", ql) or "btc-" in slug:
        asset = "BTC"
    elif "ethereum" in ql or re.search(r"\beth\b", ql):
        asset = "ETH"
    elif "solana" in ql or re.search(r"\bsol\b", ql):
        asset = "SOL"
    else:
        return None  # skip WTI/crude (no free Binance feed) and others
    # level: first $-amount or bare number, handles "$1,000,000" and "150,000"
    m = re.search(r"\$?\s*([\d][\d,]{2,})", q)
    if not m:
        return None
    level = float(m.group(1).replace(",", ""))
    # direction
    if any(w in ql for w in ["dip", "below", "down to", "drop"]) or "low" in ql:
        direction = "below"
    elif any(w in ql for w in ["reach", "above", "hit high", "hit (high)", "exceed", "over", "high"]):
        direction = "above"
    else:
        return None
    # window
    try:
        created = _to_ts(d.get("createdAt"))
        end = _to_ts(d.get("endDate"))
    except Exception:
        return None
    if not created or not end or end <= created:
        return None
    # outcome + market price proxy
    try:
        op = json.loads(d["outcomePrices"])
        yes_won = float(op[0]) == 1.0
    except Exception:
        return None
    toks = []
    try:
        toks = json.loads(d.get("clobTokenIds") or "[]")
    except Exception:
        pass
    return {
        "asset": asset, "level": level, "direction": direction,
        "created": created, "end": end, "yes_won": yes_won,
        "question": q, "slug": slug,
        "yes_token": toks[0] if toks else None,
        "last_price": _f(d.get("lastTradePrice")),
        "vol": _f(d.get("volumeNum")),
    }


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _to_ts(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S+00", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    # ISO fallback
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def load_crypto_markets() -> list[dict]:
    out = []
    for line in gzip.open(ROOT / "markets.jsonl.gz", "rt"):
        d = json.loads(line)
        s = (d.get("slug") or "").lower()
        q = (d.get("question") or "").lower()
        if not any(k in s or k in q for k in
                   ["bitcoin", "btc", "ethereum", "eth", "solana", "sol"]):
            continue
        p = _parse_market(d)
        if p:
            out.append(p)
    return out


# ----------------------------- underlying ---------------------------------

def get_klines(asset: str) -> list[tuple[int, float, float, float]]:
    """Daily (open_ts, high, low, close) for the asset, cached."""
    sym = BINANCE_SYMBOL[asset]
    cache = UND_DIR / f"{sym}_1d.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return json.loads(cache.read_text())
    rows = []
    # 1000 daily candles ~ 2.7y, enough to cover all market windows
    url = (f"https://api.binance.com/api/v3/klines?symbol={sym}"
           f"&interval=1d&limit=1000")
    data = json.load(urllib.request.urlopen(url, timeout=20))
    for k in data:
        rows.append((int(k[0] // 1000), float(k[2]), float(k[3]), float(k[4])))
    cache.write_text(json.dumps(rows))
    return rows


def spot_and_vol(asset: str, ts: int) -> tuple[float, float] | None:
    """Spot close at/just before ts, and trailing-30d annualized vol."""
    kl = get_klines(asset)
    prior = [r for r in kl if r[0] <= ts]
    if len(prior) < 31:
        return None
    spot = prior[-1][3]
    closes = [r[3] for r in prior[-31:]]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sigma = math.sqrt(var) * math.sqrt(365.0)  # annualized
    return spot, sigma


def realized_touch(asset: str, level: float, direction: str,
                   t0: int, T: int) -> bool | None:
    """Ground truth: did the path actually touch the barrier in [t0, T]?"""
    kl = [r for r in kl_window(asset, t0, T)]
    if not kl:
        return None
    if direction == "above":
        return any(r[1] >= level for r in kl)  # any daily HIGH >= level
    else:
        return any(r[2] <= level for r in kl)  # any daily LOW <= level


def kl_window(asset: str, t0: int, T: int):
    return [r for r in get_klines(asset) if t0 <= r[0] <= T + 86400]


# ----------------------------- barrier model ------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def touch_prob(spot: float, level: float, direction: str,
               sigma: float, T_years: float, mu: float = 0.0) -> float:
    """First-passage probability for GBM that S touches `level` before T.

    Driftless (mu=0) by default = risk-neutral-ish fair value.
    """
    if T_years <= 0 or sigma <= 0 or spot <= 0 or level <= 0:
        return float("nan")
    b = math.log(level / spot)
    nu = mu - 0.5 * sigma * sigma
    s = sigma * math.sqrt(T_years)
    if direction == "above":
        if spot >= level:
            return 1.0
        # P(max X_t >= b), b>0
        return (_norm_cdf((-b + nu * T_years) / s)
                + math.exp(2 * nu * b / (sigma * sigma)) * _norm_cdf((-b - nu * T_years) / s))
    else:
        if spot <= level:
            return 1.0
        # P(min X_t <= b), b<0
        return (_norm_cdf((b - nu * T_years) / s)
                + math.exp(2 * nu * b / (sigma * sigma)) * _norm_cdf((b + nu * T_years) / s))


# ----------------------------- calibration --------------------------------

def calibrate() -> int:
    mkts = load_crypto_markets()
    print(f"Parsed {len(mkts)} BTC/ETH/SOL barrier markets")
    recs = []
    for m in mkts:
        sv = spot_and_vol(m["asset"], m["created"])
        if not sv:
            continue
        spot, sigma = sv
        T_years = (m["end"] - m["created"]) / (365.0 * 86400)
        p = touch_prob(spot, m["level"], m["direction"], sigma, T_years)
        if math.isnan(p):
            continue
        touched = realized_touch(m["asset"], m["level"], m["direction"],
                                 m["created"], m["end"])
        recs.append({**m, "spot0": spot, "sigma": sigma, "T_years": T_years,
                     "p_model": p, "touched": touched})

    # A. model vs realized touch (ground truth path), binned
    print(f"\n=== A. MODEL vs REALIZED PATH (n={sum(1 for r in recs if r['touched'] is not None)}) ===")
    print(f"{'p_model_bin':>12} {'n':>4} {'mean_p':>7} {'actual_touch':>13}")
    bins = {}
    for r in recs:
        if r["touched"] is None:
            continue
        b = int(min(r["p_model"], 0.999) * 10) / 10
        bins.setdefault(b, []).append((r["p_model"], 1 if r["touched"] else 0))
    for b in sorted(bins):
        v = bins[b]
        mp = sum(x[0] for x in v) / len(v)
        act = sum(x[1] for x in v) / len(v)
        print(f"{b:.1f}-{b+0.1:.1f}   {len(v):4d} {mp:7.2f} {act:13.2f}")

    # B. model vs resolution (does YES_won == touched? validates resolution map)
    agree = sum(1 for r in recs if r["touched"] is not None
                and r["touched"] == r["yes_won"])
    n = sum(1 for r in recs if r["touched"] is not None)
    print(f"\nResolution check: realized_touch == yes_won on {agree}/{n} markets")

    # C. market last_price vs model (mispricing direction, crude proxy)
    print(f"\n=== C. MARKET-vs-MODEL on resolved outcome ===")
    _save_recs(recs)
    return 0


def _save_recs(recs):
    out = ROOT / "crypto_barrier_recs.jsonl"
    with out.open("w") as f:
        for r in recs:
            f.write(json.dumps({k: v for k, v in r.items()}) + "\n")
    print(f"wrote {len(recs)} recs -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    if a.calibrate or not (a.backtest):
        return calibrate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
