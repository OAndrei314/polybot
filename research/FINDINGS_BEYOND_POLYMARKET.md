# Findings: Beyond Polymarket — High-Yield Automated Strategy Search

Objective: find a fully-automated strategy clearing **10%+/month** (~120-200%+ APR),
accessible from Romania (EU), with disclosed drawdown/blowup risk. Prior work
(different branch/run) validated a Hyperliquid funding-basis trade at only
~5-7% net APR — too low for this objective's bar, not re-litigated here.

Stop condition: write `research/DEPLOY_THIS.md` only when a thread clears
**>5%/month net EV** (the objective's minimum bar; note the ask is 10%+/month,
so 5% net is a floor, not a target) with automation + geo-accessibility +
quantified drawdown all satisfied.

Threads are investigated in the priority order given in the objective, skipping
ahead only when a thread is cleanly dead/infeasible.

---

## Thread A: Crypto options premium selling (Deribit) — **DEAD (as tested: naive weekly short OTM puts)**

**Script:** `research/options_premium_scan.py`
**Data:** 100% real, live-pulled from Deribit public REST — no synthetic inputs.
- DVOL (Deribit's official implied-vol index) history, 12h candles, ~196 days back
  (`get_volatility_index_data`).
- Daily BTC-PERPETUAL / ETH-PERPETUAL close price, same window
  (`get_tradingview_chart_data`).
- Live order-book + real Greeks for a currently-listed ~7-10 DTE put, pulled to
  sanity-check the model against an actual tradable quote (`get_order_book`).

**Method (disclosed limitation):** Deribit's public API does not expose
historical option order-book snapshots, so the 6-month backtest is a
Black-Scholes *simulation*: real historical spot + real historical DVOL (ATM
IV) are fed into BS to price a put at the strike that model-implies a target
delta (5/10/20-delta tested); the strategy is rolled weekly (7-day hold); the
*actual* historical spot 7 days later (also real) determines assignment and
P&L. Real IV skew (OTM puts trade above ATM IV) is NOT modeled, which likely
makes the simulated premiums somewhat conservative (understated) vs. what a
real 10-delta put would have actually paid — but does not change the
fat-tail-loss dynamic that killed this thread (see below).

### Results (27 weekly rolls, ~2026-01 through 2026-07)

| ccy | delta | n | win rate | avg weekly return (on collateral) | ~monthly (additive) | worst single week |
|-----|-------|---|----------|-----------------------------------|----------------------|--------------------|
| BTC | 0.05  | 27 | 92.6% | -0.23% | -1.0% | -5.5% |
| BTC | 0.10  | 27 | 88.9% | -0.18% | -0.8% | -7.1% |
| BTC | 0.20  | 27 | 70.4% | -0.26% | -1.1% | -8.8% |
| ETH | 0.05  | 27 | 92.6% | -0.46% | -2.0% | -13.0% |
| ETH | 0.10  | 27 | 81.5% | -0.39% | -1.7% | -15.2% |
| ETH | 0.20  | 27 | 70.4% | -0.43% | -1.9% | -17.4% |

**Every configuration is net-negative on average**, despite high win rates
(70-93%). This is the textbook short-vol trap the objective explicitly warned
about: two sharp weekly BTC drawdowns in the sample (week of 2026-01-25,
spot -13%; week of 2026-05-31, spot -13%) each wiped out 20+ weeks of collected
premium in a single event. High win rate does not imply positive expectancy
when the loss tail is fat and uncapped (short puts have max loss = strike,
i.e. can lose far more than the premium collected in a single bad week).

**IV rank right now (live, not backtested):** BTC = 15th percentile
(current 38.9% vs. 196-day range [34.4%, 82.6%]); ETH = 8th percentile
(current 52.5% vs. [47.7%, 95.8%]). The strategy's own logic (objective's
Thread A thesis) says to sell premium when IV rank is *high*, not low — **IV is
currently near 6-month lows for both coins, meaning this is a bad time to
initiate this trade even if the historical net-negative result weren't
disqualifying on its own.**

**Live cross-check:** current 10-delta-ish quotes exist and are tradable —
BTC-10JUL26-59000-P (delta -0.090, mark IV 45.0%, spot 63,403) and
ETH-10JUL26-1625-P (delta -0.094, mark IV 62.7%, spot 1,792) — so the
instrument/liquidity side of this thread is not the problem; the strategy's
risk/reward itself is.

**Verdict:** DEAD as a naive/unhedged weekly OTM-put-selling strategy over the
tested 6-month window. Not retested with: higher delta targets beyond 20,
IV-rank-gated entry (only sell when rank > 70th percentile), stop-losses, or
put spreads (defined max loss) — those would be reasonable follow-ups if this
thread is revisited, but the core finding (short-vol strategies blow up on fat
left tails that erase months of premium in one week) is unlikely to flip sign
with those variants; at best they trade some upside for a smaller, still-real
tail.

**Automation score:** high — Deribit has full REST + WebSocket trading API,
no geo-block encountered (public endpoints all responded normally from this
session). Not pursued further since the underlying strategy is dead.

**Next thread:** B (leveraged Hyperliquid funding basis) — not yet investigated
this run.

---

## Thread B: Leveraged Hyperliquid funding-basis — **DEAD (survivable leverage still far below the 5%/mo floor)**

**Script:** `research/hyperliquid_leveraged_basis.py`
**Data:** 100% real, live-pulled from Hyperliquid public `/info` REST — no synthetic
inputs.
- `fundingHistory` — real hourly funding prints, 90 days, kBONK/ENA/PUMP (paginated,
  ~2160 prints/coin).
- `candleSnapshot` (1h) — real hourly close price, same window/coins.
- `meta` — real `maxLeverage` (10x for all three) and margin tiers (`marginTableId
  52`, tiered 10x up to $20M notional) — maintenance margin fraction derived as
  half of initial margin at max leverage (5%), the standard HL convention, not
  guessed.

**Correction to the objective's own framing:** the objective's Thread B prompt
asks to measure "basis divergence between HL mark price and an external oracle."
That's the wrong risk variable — HL's funding mechanism keeps mark-vs-index basis
within single-digit bps almost always, that's the entire point of funding. The
actual liquidation risk on a leveraged short-perp leg comes from **raw price
volatility of the coin itself**: the long-spot leg (held elsewhere) gains exactly
what the short perp loses when price rises, so the *combined* position is flat —
but HL's liquidation engine only looks at the perp leg's own margin account, and
your spot gains can't be transferred in fast enough during a violent move. So
this script measures realized forward price volatility (worst % rise within 1h /
4h / 24h / 7d rolling windows) against the exact liquidation-threshold formula
`(1/L - mm) / (1 + mm)` for isolated margin at leverage `L`, maintenance `mm=5%`.

### Results (90 days real data, 3 coins × 6 leverage levels)

| coin | max survivable L (no breach in any 24h window, 90d) | net APR @ that L | raw funding APR (1x) |
|------|------------------------------------------------------|-------------------|------------------------|
| kBONK | 3x (liq buffer 26.98% vs. worst 24h move 17.02%) | 6.2% | 8.2% |
| ENA   | 2x (liq buffer 42.86% vs. worst 24h move 39.20%; 3x liq buffer 26.98% < worst 24h move 39.20%, so 3x already breaches) | 4.6% | 7.0% |
| PUMP  | 3x (liq buffer 26.98% vs. worst 24h move 15.34%) | 5.9% | 7.9% |

Net APR at survivable leverage = **~4.6-6.2%/year**, i.e. **~0.4-0.5%/month** —
this is nowhere near the objective's 5%/month floor, let alone the 10%/month
target. Leverage buys very little extra yield here: capital efficiency scales
as `L/(L+1)` of the raw funding rate, so going from 1x (raw/2 = ~50% of raw APR)
to 3x (raw×0.75) only adds ~1.5-2 points of annual yield, while the liquidation
buffer collapses from 90% (1x) to 27% (3x) to 4.8% (10x) — the risk grows far
faster than the reward. Pushing past 3x is actively dangerous: at 5x, kBONK's
own 90-day history already breaches the liquidation threshold within a 24h
window (worst 24h move 17.02% > 14.29% buffer); ENA breaches at 5x within even
a 1h reaction window (worst 1h move 9.20% > 8.84% buffer at 7x, and ENA fails
24h-survivability already at 3x).

**Verdict:** DEAD for this objective's bar. Leveraging the validated Hyperliquid
funding-basis trade is a real, quantified risk/reward dial (not fake), but even
the most aggressive leverage that survived 90 days of real kBONK/ENA/PUMP price
history without a liquidation breach (2-3x, coin-dependent) only lifts net yield
to ~5-6% *annual*, not monthly. To hit 10%/month via this mechanism alone you'd
need leverage in the 40-80x range (extrapolating `L/(L+1)` toward 1), which no
HL market for these coins offers (max 10x) and which would have a liquidation
buffer near zero — guaranteed wipeout on the first normal daily candle, let
alone a violent one. This thread cannot bridge the gap; leverage is the wrong
lever for turning a ~7% APR yield trade into a 10%/month strategy.

**Automation score:** high — HL has full REST/WebSocket + Python SDKs, no
geo-block encountered. Not pursued further since the underlying yield ceiling
(not the automation) is the blocker.

**Next thread:** C (Uniswap V3 / concentrated-liquidity DEX LP) — not yet
investigated this run.

---

## Threads C-F: not yet investigated
