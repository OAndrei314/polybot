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

### Thread A follow-up: put credit spreads + trailing IV-rank gating — **still DEAD; apparent "fix" is an overfitting artifact, not an edge**

Since all 6 named threads (A-F) closed dead/infeasible by iteration 5, this follow-up
tests the two variants Thread A's own writeup flagged as untested: (1) a **put credit
spread** (short a target-delta put, buy a further-OTM put as protection — caps max
loss at a known spread width instead of "however far spot falls"), and (2) **trailing
IV-rank-gated entry** (only enter when current IV is elevated vs. its own trailing
90-day range — a no-lookahead version of the objective's own Thread A thesis, unlike
the single whole-sample "current IV rank" print in the original script, which compares
today against the *entire* historical window including future data).

**Script:** `research/options_credit_spread_scan.py` — reuses the same real DVOL +
real spot data and Black-Scholes strike-solving from `options_premium_scan.py` (short
delta 0.10, long delta 0.03, weekly rolls, same disclosed simulation limitations).

**Results (27 weekly rolls, BTC/ETH, same window as Thread A):**

| ccy | IV-rank gate | n trades | n skipped | win rate | avg monthly % | worst trade % |
|-----|-------------|---------:|----------:|---------:|---------------:|---------------:|
| BTC | ≥0% (ungated) | 24 | 3 | 91.7% | -6.9% | -93.4% |
| BTC | ≥50% | 11 | 16 | 100.0% | +29.0% | +5.4% |
| BTC | ≥70% | 9 | 18 | 100.0% | +29.0% | +5.4% |
| ETH | ≥0% (ungated) | 24 | 3 | 87.5% | -7.1% | -93.2% |
| ETH | ≥50% | 11 | 16 | 100.0% | +30.7% | +6.0% |
| ETH | ≥70% | 8 | 19 | 100.0% | +31.3% | +7.1% |

At first glance the gated rows look like a validated 10%/month+ strategy. **They are
not — this is a small-sample overfitting artifact, confirmed by inspecting the actual
per-week entry log**, not just asserted:

- **Defining a spread instead of a naked put does cap the loss, but does not fix
  expectancy.** The ungated (≥0%) row is still clearly net-negative (-6.9%/-7.1% per
  month) — same fat-tail dynamic as the naked-put version in the main Thread A result,
  just with a smaller absolute worst-case (-93% of *spread width*, not of full
  collateral). Confirms the original Thread A verdict holds for the defined-risk
  variant too.
- **The two loss weeks in the whole 27-week sample are 2026-01-25 and 2026-05-31** —
  the exact same two drawdown weeks Thread A's original writeup identified. Every other
  week in both BTC and ETH returns a small, similar positive amount (~+6.6% to +7.4%
  per trade), because in a Black-Scholes simulation with no realized skew, non-crash
  weeks are almost mechanically profitable by construction — the "edge" in the ungated
  row is entirely carried by these 2 events out of 27.
- **Both loss weeks happen to have low trailing IV-rank at entry** (BTC: rank 39 and 7;
  ETH: rank 22 and 9) — so *any* gate threshold from ~40% to ~90% excludes both events
  simply because there are only 2 tail events in the whole dataset and both happen to
  sit in the bottom third of the trailing-rank distribution. This is **not** evidence
  that "sell only when IV rank is high" is a real predictive filter for tail risk — with
  n=2 tail events, a gate can trivially be drawn to exclude both by chance, and the
  win-rate/return numbers above are a direct readout of that exclusion, not of a
  demonstrated statistical relationship. It would take many more than 2 realized crash
  events to distinguish "high trailing IV rank predicts safety" from "these two
  particular crashes happened to follow low-IV weeks." If anything, low-IV-before-a-
  crash is the opposite of the objective's own Thread A thesis (sell when IV is
  *elevated*) — it's a "calm before the storm" pattern, which is a well-known but
  separate phenomenon from vol-risk-premium harvesting, and still only n=2.
- Practically: gating removes 16-19 of 27 weeks (59-70% of the time no trade is taken
  at all), which is itself inconsistent with presenting this as a steady 29-31%/month
  strategy — most months would see zero or one trade, not a monthly-compounding return.

**Verdict:** **DEAD, unchanged.** The credit-spread structure is a real risk-management
improvement (bounded loss vs. unbounded), but does not flip the sign of a fat-tail-
negative strategy. The apparent IV-rank-gated "return" is a textbook small-n
overfitting mirage from a sample with only 2 realized tail events — not a validated
result, and should not be deployed or re-tested with the same rank-gate thresholds
without first getting a much longer (multi-year) sample with many more tail events to
actually test the "avoid entry near tail risk" hypothesis out-of-sample. Recorded here
specifically so a future iteration doesn't re-derive this same 29%/month-looking number
and mistake it for a real edge.

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

### Addendum (iteration 7): broadening the coin universe beyond kBONK/ENA/PUMP — **still DEAD, but improved best-candidate number**

The original Thread B test only checked 3 pre-picked coins with modest raw funding
APR (7-8%/yr). This addendum asks: across Hyperliquid's **full ~231-coin universe**,
does any coin currently have sustained funding high enough to change the verdict?

**Method:** `research/hyperliquid_funding_universe_scan.py` — pulled one real live
`metaAndAssetCtxs` snapshot (current hourly funding rate for all 231 listed coins),
ranked by annualized current-snapshot APR, then pulled real 90-day `fundingHistory` +
`candleSnapshot` for the top 8 positive-funding coins (plus BTC/ETH as a liquid
baseline) to check whether the live snapshot is a **sustained** rate or a
**momentary spike**, using the same liquidation-survivability method as the original
Thread B script.

**Live snapshot found a striking outlier:** GRASS at 168%/year annualized current
funding — 20x higher than anything in the original 3-coin sample, and (before
leverage) already above the ~72-84%/yr raw-APR bar this trade needs to clear
5%/month net. ZRO (70%/yr) and HEMI (49%/yr) also stood out.

**But the 90-day real history exposed GRASS and ZRO as spikes, not edges:** GRASS's
90-day mean funding APR is only 14.7% (the live snapshot was **11.46x** the sustained
mean); ZRO's 90-day mean is 17.0% (snapshot was **4.11x** the mean). Both are
momentary funding-rate blips (common after a listing, a narrative pump, or a large
one-sided positioning event), not a persistent rate a monthly-yield strategy could
count on. kLUNC and FET's snapshots didn't even match sign with their 90-day mean.

**Real results table (90-day mean funding, survivable leverage from real 90d adverse-move history):**

| coin | snapshot APR | 90d-mean APR | net APR @ safe lev | net %/month | day $ volume |
|---|---|---|---|---|---|
| GRASS | 168.4% | 14.7% | 9.8% (2x) | 0.81% | $2.4M |
| ZRO | 69.9% | 17.0% | 11.3% (2x) | 0.95% | $4.4M |
| HEMI | 49.2% | 30.9% | 20.6% (2x) | 1.72% | $0.3M |
| kLUNC | 42.0% | **-26.4%** | -17.6% (2x) | -1.46% | $0.1M |
| FET | 27.9% | **-3.2%** | -2.4% (3x) | -0.20% | $1.7M |
| XMR | 23.1% | 32.7% | 24.5% (3x) | **2.04%** | $3.8M |
| SYRUP | 22.6% | 12.4% | 8.3% (2x) | 0.69% | $0.4M |
| APEX | 17.8% | 15.3% | 11.4% (3x) | 0.95% | $0.1M |
| BTC | 11.0% | 3.6% | 3.1% (7x) | 0.26% | $1.46B |
| ETH | 11.0% | 4.0% | 3.3% (5x) | 0.28% | $470M |

**Verdict:** **DEAD**, but with a materially better honest best-candidate number.
XMR's 90-day-mean funding (32.7%/yr, positive in 98% of hourly prints) survives 3x
leverage against its own real 90-day worst-case price moves, netting **2.04%/month**
— roughly 4-5x the original 3-coin sample's best case (0.4-0.5%/month), and still the
single best number found across every thread in this entire objective. It still does
not clear the 5%/month floor. This addendum also hardens the original verdict:
scanning the *entire* coin universe (not just 3 picks) for the single highest live
funding print still tops out at ~2%/month once you filter for sustained (not
spike) rates and size leverage to real historical survivability — broadening the
coin search was a legitimate follow-up (flagged in iteration 6) and it does not
change Thread B's DEAD verdict, though it materially improves the disclosed
best-candidate number that should now replace the original 3-coin figure in
`DEPLOY_THIS.md`.

**Learning for future work:** a live single-hourly-print funding rate can be up to
~11x its own 90-day sustained mean — any future funding-rate-based thread MUST
compare a live snapshot against real historical mean before treating it as a
tradable rate, or it will systematically overstate the opportunity by an order of
magnitude on thin/newly-listed/narrative-driven coins.

**Next thread:** C (Uniswap V3 / concentrated-liquidity DEX LP) — not yet
investigated this run.

---

## Thread C: DEX concentrated-liquidity LP (Uniswap V3/V4, Orca) — **DEAD (stablecoin pairs) / NOT VALIDATED (volatile pairs, IL likely erases the edge)**

**Script:** `research/dex_lp_scan.py`
**Data:** 100% real, live-pulled from the DeFiLlama yields API (`https://yields.llama.fi/pools`,
15,855 pools total) filtered to `uniswap-v3`, `uniswap-v4`, `orca-dex` projects with
TVL ≥ $1M; cross-checked with real 30-day Hyperliquid daily candles (ETH/USD, ETH/BTC)
for the IL calculation below — no synthetic inputs anywhere.

### Stablecoin/stablecoin pairs (the objective's "near-zero IL" sub-thesis)

All 9 stable/stable pools ≥$1M TVL found (DAI-USDT, DAI-USDC, USDC-USDT ×2, PYUSD-USDE,
USDE-USDC, PYUSD-USDC, FRAX-USDC), current apy range **0.0%–1.9%**, apyMean30d range
0.2%–6.0%. Best current: DAI-USDT on Ethereum, 1.9% APY = **0.16%/month**. Best 30d-average
was USDC-USDT/Orca at 6.0% APY = 0.5%/month. **Every single stable/stable pool is at least
an order of magnitude below the objective's 5%/month floor.** IL is indeed near-zero here as
the objective predicted — but the fee yield itself is just too small on real, current,
liquid pools. Verdict: **DEAD**, not a capital/automation problem, a yield-ceiling problem.

### Volatile pairs (fee income only, IL NOT netted by DeFiLlama)

25 pools ≥$1M TVL cleared apy≥5%; best large-TVL ones: `USDC-CBBTC` (Base, $8.3M TVL, 33.8%
APY = 2.8%/month), `WETH-USDT` (Ethereum, $83.6M TVL, 21.0% APY = 1.75%/month), `USDC-WETH`
(Ethereum, $3.8M TVL, 19.3% APY = 1.6%/month). **The highest fee-APY pool at ≥$1M TVL found
in the entire scan was 39.4% (ETH-SYRUP, uniswap-v4, $1.8M TVL) = 3.3%/month — below the
5%/month floor on fee income ALONE, before any IL is subtracted.** A small number of
low-TVL (<$1M, down to $200-700k) exotic pairs (ETH-ETHFI 94.7% APY, ZEC-BTCB 60.7%,
ETH-TORN 52.2%, ETH-PORK 50.8%) nominally clear 5%/month on fees, but:
- These pools are thin (sub-$1M TVL) — a $500-5,000 position would itself move the pool's
  price meaningfully on entry/exit (slippage cost not in the apy figure), and thin pools
  carry materially higher rug-pull/exploit risk than $10M+ pools.
- DeFiLlama's `apy` field for concentrated-liquidity pools is **fee income only** — it does
  NOT net out impermanent loss. IL is a real, separate, and potentially large cost that has
  to be estimated independently.

**IL quantification (real data, not theoretical):** using real Hyperliquid daily closes,
the last 30 days saw ETH/USD move only +1.3% net but swing **14.6%** high-to-low intramonth
(a very ordinary month, not a crash). Standard constant-product IL formula
(`IL = 2√k/(1+k) - 1`, k = price ratio change) gives full-range IL of only -0.23% for that
swing — but concentrated-liquidity ranges (which is *why* these pools show 15-40% fee APY
instead of v2's typical 2-5%) amplify both fees AND IL roughly in proportion to how tight the
range is. For a realistic ±5% active range (needed to earn top-quartile fee APY), the rough
concentration multiplier (~20x vs full-range) implies **~-4.6% IL from an ordinary 14.6%
intra-month price swing** — comparable to or larger than an entire month's fee income for
even the best pools found, and the position would also need active rebalancing (itself a
cost) once price exits the range. Cross-pair correlation matters too: ETH/BTC-denominated
pools (WBTC-WETH, $31.9M TVL, 10.6% APY) had a far smaller real 30-day ratio move (9.8%,
IL ≈ -0.11% full-range) since ETH and BTC are highly correlated — but that pool's fee APY
(10.6% = 0.9%/month) is itself below the floor even before IL.

**Verdict:** **DEAD** for stablecoin/stablecoin pairs (yield ceiling, not a risk problem).
**NOT VALIDATED / likely net-negative-to-marginal** for volatile-pair concentrated LP —
every pool with real $1M+ TVL that clears 5%/month on fees alone was not found (ceiling is
~3.3%/month at scale); the handful of sub-$1M exotic pools that nominally clear it carry an
IL cost of comparable magnitude to the fee income from just an *ordinary* month's price
swing (not even a crash), the same fat-tail/erasure dynamic that killed Thread A, plus
un-netted slippage and thin-pool exploit risk. Not pursued into a live backtest since the
fee-vs-IL math already disqualifies it at this objective's bar without needing one.

**Automation score:** high — Uniswap V3/V4 (Ethereum/Arbitrum/Base) and Orca (Solana) both
have full read/write APIs (web3.py / solders+anchorpy), rebalancing is scriptable. Not
pursued further since the underlying yield-after-IL ceiling (not the automation) is the
blocker.

**Next thread:** D (CEX-DEX arbitrage, Hyperliquid vs on-chain spot) — not yet investigated
this run.

---

## Thread D: CEX-DEX arbitrage (Hyperliquid mark vs on-chain/oracle spot) — DEAD

**Methodology (real, live data, no simulation):** Hyperliquid's own `metaAndAssetCtxs`
endpoint returns, per coin, both `markPx` (the perp mark price — what a trade actually
executes against) and `oraclePx` (HL's external-price oracle, a median across venues —
this **is** the "on-chain spot" reference the objective's Thread D asks for, so no separate
Pyth/CoinGecko call was needed to get an apples-to-apples number). `research/cex_dex_arb.py`
polled this endpoint every 5s for a live 120-second window across 6 coins — BTC/ETH/SOL
(deep, liquid) and kBONK/ENA/PUMP (thin, high-beta, the same coins Thread B flagged as
highest-volatility) — computing `(mark-oracle)/oracle` each sample, plus a one-shot real
`l2Book` best-bid/ask spread per coin as the execution-cost floor.

**Results (144 coin-samples, live 2026-07-04):**

| coin  | mean spread% | max \|spread\|% | book spread% | samples >0.3% |
|-------|-------------:|----------------:|-------------:|--------------:|
| BTC   | -0.018%      | 0.032%           | 0.0016%      | 0 / 24        |
| ETH   | +0.014%      | 0.017%           | 0.0056%      | 0 / 24        |
| SOL   | -0.007%      | 0.018%           | 0.0012%      | 0 / 24        |
| kBONK | -0.002%      | 0.020%           | 0.0200%      | 0 / 24        |
| ENA   | -0.034%      | 0.052%           | 0.0126%      | 0 / 24        |
| PUMP  | +0.061%      | 0.064%           | 0.0636%      | 0 / 24        |

**Largest instantaneous \|mark-oracle\| spread observed across all 144 samples: 0.064%** —
under a quarter of the 0.3% trigger the objective's own math requires to be worth trading,
and this held even for PUMP/kBONK/ENA (thin, high-beta alts, the coins most likely to show a
gap). Zero samples anywhere near the threshold. The tightest real order-book spread alone
(0.0012-0.064%, i.e. the cost of instantly entering+exiting just the HL leg) already consumes
a meaningful fraction of the 0.3% trigger before the other leg's (CEX or on-chain) own
spread, gas, and slippage are even added.

**Disclosed limitation:** this is a 2-minute live snapshot, not a multi-day distribution —
it cannot statistically rule out rare spike events during extreme volatility (e.g. a flash
crash or major news print). But it directly corroborates Thread B's independent finding from
90 days of real hourly `fundingHistory` data (iteration 2): HL's funding mechanism plus
external arbitrageurs keep mark≈oracle within single-digit bps essentially always, for the
same coin set, including the thin/high-beta ones. Two independent real-data measurements
(hourly-averaged over 90 days, and instantaneous over a live 2-minute window) now agree: the
gap this thread needs (>0.3%, held long enough to execute both legs) is not a "sometimes it
happens, capture it" event on Hyperliquid — it's a structurally-closed gap, by design of how
HL's own price mechanism works. Extended live monitoring (days/weeks) could only find rarer,
smaller, and shorter-lived divergences than the 120s window already showed a ceiling for —
not a reason to build that infra given the other 3 dead threads already found in this run.

**Automation score:** high (a 5s-poll Python loop is trivial) — irrelevant here, since there
is no measurable edge to automate.

**Verdict:** **DEAD.** Consistent with Threads A-C: every mechanism tested so far that has a
plausible, automatable capture path turns out to be either arbitraged away already (this
thread), fee-ceiling-bound (Thread C), or fat-tail-negative in expectancy (Thread A), or too
small even with maximum safe leverage (Thread B).

**Next thread:** E (Kalshi/Polymarket via VPN) and F (Solana liquidation bots) — not yet
investigated this run.

---

## Thread E: Kalshi / Polymarket via VPN + prediction-market automation — **DEAD / INFEASIBLE (legal & regulatory risk)**

**Method:** No code — pure legal/technical feasibility check via primary-source and
news research (Kalshi Member Agreement, Polymarket geoblock docs, Manifold docs, and
recent EU regulatory-enforcement reporting), per the objective's own instruction that
this thread only needs a feasibility read, not a backtest.

**Findings (real, current as of 2026-07-04):**
- **Kalshi**: CFTC-regulated, expanded to 140+ countries as of Oct 2025. Secondary
  sources list Romania as *not* on its 54-country restricted list (Belgium, France,
  Italy, Poland, UK, Bulgaria, Hungary, Switzerland, sanctioned states are barred) —
  but this is second-hand, not confirmed against the primary Member Agreement PDF, and
  the EU regulatory environment is actively hardening: 9 European regulators (Belgium,
  France, Germany, Italy, Netherlands, Poland, Portugal, Spain, Switzerland) issued a
  June 2026 joint statement pledging coordinated enforcement against unlicensed
  prediction markets (naming Kalshi and Polymarket), and Spain has already blocked both.
  KYC requires real government ID + declared country of residence (no VPN-friendly
  path); funding is card/wire/crypto only. Public market-data endpoints are open and
  unauthenticated globally, but that's data-only, not a trading edge.
- **Polymarket via Tor/VPN**: no documentation confirms Tor reliably bypasses the
  geoblock, and it's moot regardless — Romania's gambling regulator (ONJN) has
  explicitly blacklisted Polymarket (citing $600M+ in election-market trading during
  Romania's May 2026 elections), Polymarket's ToS explicitly forbids circumvention
  tools, and real-money trading requires KYC'd wallet verification that VPN/Tor cannot
  spoof — funds are frozen on detected location mismatch independent of IP.
- **Manifold Markets**: confirmed play-money only (real-money "sweepcash" sunset March
  2025) — freely accessible from Romania but irrelevant to a real-yield objective.

**Verdict:** **DEAD.** No option combines "real money," "legally clean," and "low
regulatory-tail-risk" for a Romanian resident today. Kalshi's Romania eligibility
rests on unconfirmed secondary sources inside a rapidly-tightening EU enforcement
climate that could add Romania to the restricted list at any time — not a foundation
to automate capital against. Even in the best case (Kalshi genuinely open to Romania
today), this is a market-access question, not a yield source — it doesn't independently
supply a 5%/month edge, it would only reopen the 31-already-dead Polymarket-style
threads on a different prediction-market venue with a live account.

**Automation score:** N/A — blocked before the automation question is reached.

**Next thread:** F (Solana MEV / liquidation bots) — last thread in the objective's
priority list.

---

## Thread F: Solana MEV / liquidation bots (marginfi / Drift / Kamino) — **NOT VALIDATED (data-infeasible + adverse real-world event)**

**Method:** Attempted real-data pulls against every avenue the objective names, no
synthetic inputs used anywhere in this section:
1. Drift's documented Data API (`docs.drift.trade/developers/data-api`) — the docs URL
   redirects (307) to `docs.velocity.exchange/developers/data-api`; that page states
   Velocity's hosted Data API host is "not yet finalized," so there is currently no
   live documented endpoint to query for liquidation records.
2. Drift's known historical-data S3 bucket
   (`drift-historical-data.s3.eu-west-1.amazonaws.com/program/<programId>/liquidationRecords/...`)
   — every guessed key path returned `403 AccessDenied` (bucket/prefix requires exact
   per-user-account keys or is no longer public).
3. `data.api.drift.trade` (the pre-rebrand host) — returns `403 Forbidden` on the root.
4. marginfi docs — no documented historical-liquidation-stats REST endpoint found;
   only a third-party Dune dashboard (`dune.com/danielswiss/marginfi-liquidations`)
   was located, which 404s on unauthenticated fetch (Dune dashboards are
   JS-rendered/query-gated, not scrapable without an API key + paid credits).

**Critical real-world finding:** Drift Protocol — the primary venue this thread names
— has been **offline since April 1, 2026** after a suspected DPRK/Lazarus-linked
exploit drained **~$285-295M** via a months-long social-engineering attack on its
multisig Security Council (durable-nonce pre-signed transactions were hijacked to
grant the attackers admin control, which was then used to whitelist a fake token as
infinite-borrow collateral). Drift has since rebranded to **Velocity DEX**, backed by
a $127.5M Tether credit line, and is running only a **private beta** on a freshly
deployed program with rotated keys — no public relaunch date beyond "coming days" as
of this research (2026-07-04). A liquidation bot cannot be built against a protocol
that is not in normal live operation, and the pre-hack historical liquidation pattern
on the old program is not representative of the rebuilt system's parameters, collateral
set (USDT replacing USDC), or risk limits.

**Secondary evidence (marginfi/Kamino, still live):** a practitioner writeup
(baransel.dev) building a real marginfi/Drift liquidation bot reports per-liquidation
profit in the **$15-500** range (5-10% bonus on the repaid debt, size-dependent), but
explicitly gives **no aggregate daily/monthly volume or win-rate statistics**, and
states their own bot **lost ~60% of liquidation races** to faster/better-resourced
competing bots even after building it, requiring paid staked Jito RPC infrastructure
and priority fees on every attempt (sunk cost win or lose). No public dashboard
(Dune/Flipside) was reachable without an API key to establish a real frequency
baseline.

**Verdict:** **NOT VALIDATED.** This is the only thread in the full A-F set where the
blocker isn't "the math says no" but "the data needed to do the math isn't obtainable
for free, and the flagship venue is non-operational right now." What real evidence
exists points to a bursty, event-driven, latency-competitive MEV race (not a steady
monthly-yield strategy) where a solo Python retail bot is very plausibly the *slowest*
participant, not the fastest — the opposite risk profile of the 5-10%/liquidation
headline number. Cannot responsibly claim this clears 5%/month without a live,
paid-RPC, multi-week measurement this iteration didn't build (and won't, given Drift's
outage removes the most liquid named venue and the marginfi/Kamino path still can't be
quantified from public data).

**Automation score:** technically high (Python + solders/anchorpy can watch oracle
updates and submit liquidation instructions) — moot without a quantified positive-EV
signal and while paying for infra to compete against better-capitalized searchers.

**This closes the objective's full A-F priority list.** All six named threads are now
DEAD or NOT VALIDATED under real-data testing. See the final verdict and best
candidate in `research/DEPLOY_THIS.md`.

---

## Thread B addendum #3: cross-exchange perp-perp funding arbitrage (iteration 15)

**Concept (new mechanism, flagged as a follow-up in iteration 6, not in the objective's
original A-F list):** instead of Thread B's spot-long + Hyperliquid-perp-short
construction, go perp-vs-perp on the SAME coin across TWO exchanges (Hyperliquid and
Bybit) — long whichever side has the lower/negative funding, short the side with the
higher funding. This collects the funding-rate *differential* between venues rather
than a single venue's absolute rate, and swaps Thread B's specific risk (spot leg
sitting in a different wallet from margin, unable to be transferred fast enough during
a violent move) for a different one (both legs are now leveraged/marginable positions,
each independently liquidatable, though tracking each other tightly since both are
perps arbitraged against a similar spot).

**Method (100% real data):** `research/cross_exchange_funding_arb.py` — pulls live
current funding for HL's full ~231-coin universe (`metaAndAssetCtxs`) and Bybit's full
~610-base linear-perp universe (`/v5/market/tickers`, annualized per-symbol using each
coin's own `fundingIntervalHour`, which varies 1h-8h), matches coins across both naming
schemes (handling HL's `k`-prefix vs Bybit's `1000`-prefix multiplier convention), then
validates the top live-differential candidates against 30 real days of funding history
from both venues (HL `fundingHistory`, Bybit `/v5/market/funding/history`, both
cursor-paginated) using the same spike-detector methodology validated in iteration 7.

**Result:** the live snapshot scan's #1 hit (STG, apparently -466%/yr sustained
30d-mean differential, which would have been ~38.9%/month) was a **data artifact, not
an opportunity** — Hyperliquid's `metaAndAssetCtxs` confirmed `STG` carries
`isDelisted: True` and `openInterest: 0.0`, meaning its funding rate is permanently
pinned at `0.0` because the market is untradeable, not because it's a real yield.
Added an explicit delisted/zero-open-interest filter (55 of 231 HL "coins" were
filtered out on this basis) before re-running. After that fix, every one of the top-10
live-differential candidates was flagged as a spike (live snapshot 3x+ its own 30-day
mean, same trap as GRASS/ZRO in iteration 7) **except STRK**, whose real 30-day-mean
differential (15.0%/yr) nets **1.25%/month unlevered** — below even Thread B's
single-venue XMR figure (2.04%/month). Cross-exchange price-tracking risk for STRK was
also measured directly (720 real aligned hourly price samples from both venues over
30 days): mean spread 0.068%, p99 0.266%, max 0.445% — tight enough that this specific
risk (both legs diverging in value during a violent move) is not the binding
constraint; the yield itself is.

**Verdict:** **DEAD.** Cross-exchange perp-perp funding arb does not clear 5%/month
(best real, non-spike, non-delisted candidate: 1.25%/month) and does not even beat the
already-disclosed single-venue XMR candidate from Thread B's iteration-7 addendum
(2.04%/month) — broadening from one venue to two, and from spot+perp to perp+perp,
did not find a materially better opportunity, and surfaced a new data trap (delisted
markets with pinned-zero funding masquerading as extreme opportunities) worth
remembering for any future funding-differential scan. `research/DEPLOY_THIS.md`'s
best-candidate section is unchanged (XMR, Thread B, 2.04%/month, still sub-bar).

---

## Thread G (new mechanism, iteration 16): Deribit dated-futures cash-and-carry basis — **DEAD**

**Concept (not in the objective's A-F list; distinct from Thread B):** buy spot
(or long the perpetual as a spot proxy), short a dated (weekly/monthly/quarterly)
future at a premium to index, hold to expiry. Unlike Thread B's perpetual-funding
basis (yield paid/collected periodically at a variable rate), a dated future's
basis is priced once at entry via the forward curve and converges deterministically
to zero at settlement — a genuinely different risk/mechanism (no funding-rate
variability, but capital is locked until expiry, and index/settlement risk applies
at the specific settlement instant).

**Method (100% real, live data):** `research/deribit_futures_basis.py` —
`GET /public/get_instruments` (kind=future, expired=false) for BTC and ETH's full
dated curve out to Jun 2027, `GET /public/get_index_price` for live spot index,
`GET /public/get_book_summary_by_instrument` for live mark price per contract to
compute annualized basis at every point on the curve, plus a 90-day historical
check (`get_tradingview_chart_data` on the front quarterly contract vs the
perpetual, which Thread D already confirmed tracks index within bps) to see
whether today's basis is a low snapshot or representative of the recent past.

**Result (live, 2026-07-05):**
- BTC curve: annualized basis rises smoothly from ~0% (1-week) to only **4.02%/yr**
  at the farthest listed contract (BTC-25JUN27, 355 days out) → **0.33%/month**.
- ETH curve: same shape, tops out at **3.18%/yr** (ETH-25JUN27) → **0.27%/month**.
- 90-day history of the front quarterly (25SEP26) vs perpetual: BTC ranged
  1.75%-3.73%/yr annualized (mean 2.67%), ETH ranged 0.95%-3.17%/yr (mean 2.13%)
  — today's snapshot is representative of the last 3 months, not a low outlier.
  This market is in persistent, thin contango, not the 20-50%/yr contango seen
  in some past bull-market cycles.

**Verdict:** **DEAD.** Even the longest-dated (1-year-out) contract on either
coin doesn't clear 1%/month, let alone the 5%/month floor — two orders of
magnitude short. Locking capital for up to a year to collect ~0.3%/month also
fails the "capital efficiency" criterion independent of the yield shortfall.
Confirms (via an independent instrument/mechanism) the same conclusion as
Thread B: the current market environment (mid-2026) simply does not have a
material market-neutral crypto basis anywhere on the curve, whether collected
via periodic perpetual funding or via a dated-futures forward premium.
`research/DEPLOY_THIS.md`'s best-candidate section is unchanged (XMR, Thread B,
2.04%/month via perpetual funding, still the best real number found, still
sub-bar).

---

## Thread F re-test (iteration 17): direct on-chain marginfi liquidation measurement — closes the data gap, hardens NOT VALIDATED

Iteration 5 marked Thread F **NOT VALIDATED** specifically because every named
data source was blocked (Drift Data API 403/not-finalized, Drift historical-data
S3 bucket 403, marginfi's Dune dashboard 404 without a paid key) — the verdict
was "can't quantify," not "quantified and dead." This iteration closes that gap
by going around all three blocked sources and reading real liquidation activity
**directly from Solana mainnet** via the public JSON-RPC (`api.mainnet-beta.solana.com`,
no API key, no paid infra) — a data path not attempted in iteration 5.

**Script:** `research/solana_liquidation_scan.py` — confirmed marginfi-v2's mainnet
program id (`MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA`, via docs.marginfi.com /
mrgnlabs/marginfi-v2 GitHub), pulled real `getSignaturesForAddress` history for that
program (2000 signatures spanning a real, measured 2.496-hour window), sampled 140
real transactions via `getTransaction` (80 successful + 60 failed) and scanned full
log output for the string "liquidat", then ran a second, differently-biased sample
(200 transactions, every 4th signature from a fresh 1000-signature pull, classified
by the top-level `Instruction:` log line only) to cross-check.

**Result — liquidations are a vanishingly small fraction of this program's real
on-chain traffic:**
- Sample 1 (140 txs, full-log keyword scan): only **1 transaction** mentioned
  "liquidat" anywhere in its logs, and that one **failed** (lost the race) —
  0/80 successful liquidations found.
- Sample 2 (200 txs, top-level-instruction classification): **0/200** had a
  liquidation instruction at the top level. The actual dominant instruction was
  `LendingAccountStartFlashloan` (186/200 = 93%), i.e. flash-loan arbitrage bots,
  not liquidators.
- **96.0% of all 2000 sampled signatures failed** (`err != null`) — and per
  Sample 2's breakdown, this failure-storm is overwhelmingly flash-loan-arb bots
  reverting (`InsufficientFunds`, custom program errors after a
  `Borrow`→`Swap`/`SwapV2`→repay sequence), not liquidation races specifically.

**Verdict: NOT VALIDATED, now on hardened real-data grounds instead of blocked
APIs.** Two things changed from a "no data" verdict to a "the data says no"
verdict:
1. **Liquidation events are rare relative to total program traffic** (≤1 in
   ~340 sampled transactions across two independent samples) — this is not a
   steady several-times-a-day income source the objective's illustrative math
   ("5 liquidations/day at $100 profit each") assumed; real frequency looks
   like at most a handful per day, plausibly less, on marginfi specifically.
2. **The ecosystem around this exact program is already saturated with
   automated, latency-sensitive bots** — 93% of all its transaction volume is
   flash-loan arbitrage attempts, 96% of which fail outright. That failure rate
   is direct, real, on-chain proof of exactly the "many bots racing the same
   opportunity, most lose" dynamic iteration 5's secondary source (baransel.dev,
   a practitioner losing ~60% of liquidation races) only had anecdotal evidence
   for. If the *lower-value, higher-frequency* adjacent opportunity (flash-loan
   arb) is already this contested, a new, non-paid-RPC, retail Python bot
   entering the *rarer, higher-value* liquidation niche should expect to be
   competing against the same or better-resourced incumbents, not finding an
   open lane.

This does not reverse Thread B's standing best-candidate figure (XMR,
2.04%/month) — it closes out the one thread (F) that iteration 5 could not
fully quantify, converting it from "blocked" to "measured and still dead,"
with the added finding that the mechanism's competitive intensity is real and
severe, not merely asserted. `research/DEPLOY_THIS.md` updated to reflect this
harder verdict on Thread F.

**Learning:** when a target program/protocol's documented data APIs are all
blocked (403/404/paywalled), the raw public RPC (`getSignaturesForAddress` +
`getTransaction`, no key needed) is a viable fallback for measuring real
on-chain event frequency for ANY Solana program — logs expose the human-readable
`Instruction: <Name>` line, which is enough to classify transaction *type*
(though not USD profit, which would need pre/post token-balance diffing, not
attempted here since frequency alone already settles the verdict).

---

## Thread H (iteration 18, new mechanism beyond A-F): Jupiter Perpetuals (JLP) counterparty-vault yield

**Concept:** JLP is a single Solana token representing a pooled vault that takes
the *other side* of every leveraged trade on Jupiter Perpetuals. This is a
genuinely different mechanism from every prior thread: not funding, not basis,
not options premium, not LP fees on a spot AMM — it's a real counterparty
position against traders (LPs profit when traders lose, which they mostly do
over time), paying out ~75% of trading + borrow fees. It's widely marketed
(Jupiter's own materials, multiple secondary sources) as "30-60% APY," and is
fully automatable (buy-and-hold or DCA via any Solana wallet/RPC, no active
management required) and accessible from Romania (on-chain, no geo-block).

**Critical wrinkle the marketing figure hides:** JLP's disclosed target
composition is only ~64% stablecoins-equivalent-free — it's roughly 44% SOL,
11% BTC, 9% ETH, and 36% USDC/USDT (per Jupiter's own station page and
cross-checked secondary sources). That means JLP's USD price carries real,
large, undiversified long-crypto beta on top of whatever fee/counterparty
yield it earns. The advertised "30-60% APY" is a fee-generation-rate figure
(fees collected / AUM, annualized) — it is **not** the total return a real
USD-denominated holder experiences, because it doesn't net out the vault's own
asset-price exposure or its P&L as counterparty to traders.

**Script:** `research/jlp_perps_vault_scan.py` — pulls real 180-day daily USD
price history (CoinGecko) for JLP itself and for its three volatile
constituents (SOL, BTC, ETH), builds a static-weight synthetic "hold the raw
basket, no vault mechanics" benchmark using the disclosed target weights, and
computes JLP's real total return vs. the benchmark's return at 30/90/180-day
windows. The gap between the two (`excess`) isolates the actual fee-vs-trader-PnL
yield component from pure crypto-beta.

**Result (real data, pulled 2026-07-05, cached in `data/jlp_vault_scan_cache.json`):**

| Window | JLP total return | SOL | BTC | ETH | Basket (44/11/9 wtd) | Excess (JLP − basket) |
|---|---|---|---|---|---|---|
| 30d | +9.21% | +18.10% | -1.30% | +0.15% | +7.83% | **+1.38%** |
| 90d | +0.28% | -0.72% | -8.73% | -15.99% | -2.71% | **+3.00%** |
| 180d | **-23.50%** | -42.44% | -32.78% | -46.23% | -26.44% | **+2.94%** |

- Over the last 6 months, a real USD-denominated JLP holder **lost 23.5% of
  their capital** — not because the vault mechanism failed, but because SOL/BTC/ETH
  each fell 33-46% over the same window (a real broad crypto downturn) and JLP's
  ~64% exposure to that basket dominates the return.
- Netting out that beta (JLP return minus the static basket's return) gives the
  actual "yield" the counterparty/fee mechanism contributed: **+1.38%, +3.00%,
  +2.94%** at the 30/90/180-day windows respectively — consistent across all
  three windows at roughly **0.5-1.4%/month additive**, i.e. materially below
  both the 5%/month floor and Thread B's standing XMR figure (2.04%/month).
- The 30-60% "APY" marketing figure and this scan's measured excess are not
  contradictory — the marketing figure is a fee-side annualization (real fees
  collected, annualized, at a point in time) while this scan measures what a
  holder's wallet balance actually did in USD, net of the vault's own directional
  exposure. Both can be true simultaneously; only the latter matters for
  answering "did holding JLP make money net of crypto beta."
- A fully-hedged variant (long JLP, short SOL/BTC/ETH perps in the disclosed
  target proportions to strip the beta) is theoretically possible and would
  isolate the ~0.5-1.4%/month excess — but that's already sub-bar before
  accounting for perp funding/borrow costs on the hedge legs, which would likely
  consume most or all of the thin excess. Not built, since the un-hedged ceiling
  already fails to clear the floor.

**Verdict: DEAD (as an unhedged buy-and-hold), and NOT WORTH HEDGING.** The
un-hedged JLP position is a real, live, automatable, Romania-accessible asset,
but its true 180-day performance (-23.5%) was a large real loss driven by
crypto beta, not a 30-60%/year yield. The isolated non-beta "yield" component
(~0.5-1.4%/month across three independent windows) is consistent but still
below Thread B's existing best candidate (XMR, 2.04%/month), and hedging away
the beta to actually capture it would add cost/complexity likely exceeding the
excess itself. `research/DEPLOY_THIS.md` best-candidate figure (XMR,
2.04%/month) is unchanged.

**Learning:** any DeFi "real yield" or vault-token product that holds a mix of
volatile assets (not just stablecoins) has its headline "APY" computed from fee
generation alone, not from what a USD holder's balance actually did — these two
numbers can diverge by 20+ percentage points in either direction depending on
whether the underlying basket had a good or bad few months. Any future thread
evaluating a similar counterparty-vault or "real yield" token (e.g. GMX
GM pools, which showed 0-27% *fee-only* APY per pool in this iteration's
DeFiLlama pull and carry the same caveat) must decompose total USD return
against a no-mechanism benchmark before treating the advertised APY as
investable, exactly as done here.

---

## Thread I (iteration 19, new mechanism beyond A-H): Ethena sUSDe multi-venue delta-neutral basis yield

**Concept:** Ethena's USDe is a synthetic dollar backed by a delta-neutral
basis book: long staked-ETH/BTC/stablecoin collateral, short equal-notional
perps spread across *multiple* centralized exchanges simultaneously (Binance,
Bybit, OKX, Deribit, etc), plus collateral staking yield. Staking USDe as
sUSDe passively earns that combined income stream. This is a distinct
mechanism from Thread B (single-venue Hyperliquid funding) and Thread B's
addendum #3 (HL-vs-Bybit cross-exchange arb) because it is a *diversified,
professionally-run* basis book across many venues at once, plus a staking-yield
component on the collateral — it tests "can we just buy someone else's
already-diversified basis yield" rather than "can we replicate/beat a
single-venue basis trade ourselves."

**Script:** `research/ethena_susde_scan.py` — 100% real, live DeFiLlama public
yields API (no key needed): live snapshot of the `ethena-usde` SUSDE pool +
Aave v3 USDE/SUSDE lending markets (for loop-leverage economics), plus the
**full real daily history since the pool's 2024-02-16 DeFiLlama listing**
(871 samples) via `/chart/{pool}`.

**Result (real data, pulled 2026-07-05):**

| Window | n | mean APY | mean %/mo | range |
|---|---|---|---|---|
| 30d | 31 | 3.86%/yr | 0.32%/mo | [3.52%, 4.58%] |
| 90d | 91 | 3.96%/yr | 0.33%/mo | [3.16%, 5.71%] |
| 180d | 181 | 3.95%/yr | 0.33%/mo | [3.16%, 5.71%] |
| 365d | 366 | 4.90%/yr | 0.41%/mo | [0.13%, 10.29%] |
| full history (since 2024-02) | 871 | 10.00%/yr | — | [0.00%, 55.87%] |

- The eye-catching 24-55%/yr readings are all clustered in the 2024-02→2024-06
  launch window (novelty period + heavy points/incentive farming demand for
  USDe supply, which pushed short-side funding rates up market-wide). Since
  2025-01 (18 straight months), the rate has been steady and structurally low,
  **3.5-5%/yr (~0.3-0.4%/month)** — materially below both the 5%/month
  objective floor and Thread B's standing best candidate (XMR, 2.04%/month
  ≈ 24.5%/yr).
- **Loop-leverage does not rescue this trade.** Live sUSDe base yield is
  3.82%/yr; the live Aave USDe *supply* APY (4.93%/yr) is a floor/proxy for
  the real *borrow* cost of USDe (Aave borrow rates are structurally ≥ same-asset
  supply rates) — since even this generous floor already exceeds the base
  yield being levered, every loop iteration is net-negative before even
  counting DEX-swap fees/slippage per loop. Modeled leverage: 1x = 3.82%/yr,
  2x = 2.71%/yr, 3x = 1.60%/yr, 5x = **-0.62%/yr**. Leverage makes it worse,
  not better, because the spread (base yield − borrow cost) is already
  negative at the margin.

**Verdict: DEAD.** A third independent real-data source (after HL's own
funding history in Thread B, and Deribit's forward curve in Thread G) now
confirms the same market-regime fact from yet another angle: crypto-market-wide
delta-neutral basis yield (however it's harvested — single-venue perp funding,
cross-venue perp funding, dated-futures cash-and-carry, or a diversified
multi-CEX vault like Ethena) is currently thin (~4-5%/yr) almost everywhere,
not just on the specific venues/coins tested in earlier threads. This is now
7 post-A-F follow-up mechanisms tested (iterations 6, 7, 15, 16, 17, 18, 19),
all DEAD/NOT VALIDATED; standing best candidate remains XMR funding basis
(Thread B, 2.04%/month) — unchanged by this result.

**Learning:** buying a large, already-diversified "real yield" product
(Ethena/sUSDe, $1.6B+ TVL, professionally hedged across many CEXs) is a
legitimate way to sanity-check whether a niche single-venue finding (e.g.
XMR on Hyperliquid) is an exploitable inefficiency or just noise riding a
temporarily-elevated coin-specific rate — here it confirms the broader
regime (thin basis yield market-wide) rather than suggesting our XMR figure
is somehow leaving money on the table elsewhere.

---

## Thread J (iteration 20, first directional mechanism beyond A-I): Leveraged trend-following / momentum on Hyperliquid perps

**Why this thread:** every prior thread (A-I, 13 mechanisms total across 6
named threads + 7 post-A-F follow-ups) tried to harvest a MARKET-NEUTRAL
spread and all landed below 5%/month — the current regime has thin basis
almost everywhere. The objective explicitly permits high-risk directional
strategies ("do not filter out high-risk strategies, just disclose risk
clearly"), and no prior thread had tested a directional bet. This is the
first genuinely directional mechanism tested in this objective.

**Method (100% real data):** `research/momentum_trend_scan.py` pulls real
Hyperliquid daily `candleSnapshot` closes for BTC/ETH/SOL (700 days, the
full available HL perp history back to inception, 701 daily closes each)
and backtests a fully mechanical, always-in-market EMA-crossover
trend-following rule (long when fast EMA > slow EMA, short otherwise, flip
position on cross) with a 3.5bps taker fee charged on each flip. No
per-coin curve-fitting — one fixed rule tested identically on all three
coins, then re-run at 3 different EMA window pairs (10/30, 20/50, 5/20) to
check robustness rather than reporting only the best-looking
parameterization. Leverage (1x/2x/3x/5x) applied by directly multiplying
daily returns before compounding, with liquidation checked against each
leverage's exact isolated-margin threshold (same formula as Thread B) vs.
the real worst single-day adverse move in the window.

**Results (real data, 2026-07-05, EMA 10/30 base case):**

| Coin | Lev | CAGR | Monthly-equiv | Max drawdown | Sharpe | Liquidated? |
|---|---|---|---|---|---|---|
| BTC | 1x | 30.7% | 2.22%/mo | -33.5% | 0.82 | No |
| BTC | 2x | 39.6% | 2.78%/mo | -59.4% | 0.82 | No |
| BTC | 3x | 21.5% | 1.62%/mo | -83.0% | 0.82 | No |
| BTC | 5x | -52.5% | -5.94%/mo | -98.9% | 0.82 | No |
| ETH | 1x | 18.0% | 1.37%/mo | -47.2% | 0.59 | No |
| ETH | 2x-5x | negative | negative | -77% to -100% | 0.59 | No |
| SOL | 1x | -32.5% | -3.17%/mo | -71.5% | -0.09 | No |
| SOL | 5x | ruin | ruin | -100.1% | -0.09 | Yes |

Robustness check across EMA(20/50) and EMA(5/20) (1x/2x only): best 1x
monthly-equivalent across all three parameterizations and three coins never
exceeded 2.47%/month (ETH, EMA 5/20), and BTC/SOL were frequently negative
or near-zero (e.g. BTC EMA 20/50 at 1x: only 0.29%/month). 0/24 total
(coin × leverage × parameterization) rows across all three runs cleared
5%/month while surviving their own worst historical day.

**Verdict: DEAD.** Two independent, compounding findings:
1. Even the *best* case found (BTC, 1x, EMA 10/30) only reaches
   2.22%/month — below the 5%/month floor, and roughly comparable to (not
   better than) the standing XMR market-neutral candidate (2.04%/month),
   but with vastly worse risk (-33.5% max drawdown vs. XMR's bounded
   liquidation-buffer math).
2. **Leverage makes a leveraged, daily-rebalanced directional strategy
   WORSE, not better**, beyond a low threshold (2x on BTC, and already
   net-negative by 2x on ETH/SOL) — this is volatility drag/compounding
   decay: daily-rebalancing a leveraged position on a volatile asset erodes
   the geometric return even when the underlying trend signal is
   directionally correct, because variance costs compound every day while
   the raw directional edge only pays off net of that drag. SOL at 5x hit
   outright ruin (equity toward zero) within the 700-day window in the
   base-case run.

**Risk disclosure:** max drawdowns of -33% to -100% at every leverage level
tested (including 1x) make this categorically higher-risk than every
market-neutral thread tested so far, for a return that doesn't even clear
the bar those threads also missed. This is a genuine "high risk, still not
enough reward" result, not merely "high risk, high reward you might not
want" — the objective's directional-risk door was open, and the real
numbers walked through it and still came up short. Automation would be
straightforward (Python + HL API, same infra as Thread B), but there is no
tested parameterization of this rule worth deploying at any leverage.

**Verdict summary:** DEAD. Standing best candidate across all 14
mechanisms now tested (6 named + 8 post-A-F follow-ups) remains XMR
funding basis (Thread B, iteration 7, 2.04%/month) — it is not just the
highest-yield candidate found, it is now also demonstrably lower-risk
(bounded liquidation math, no historical drawdowns anywhere near -30%+)
than the one directional alternative tested. `research/DEPLOY_THIS.md`
updated with a Thread J summary; best-candidate figure unchanged.

**Learnings:**
- This objective's search had an implicit blind spot: 13 straight
  mechanisms (A-I) were all market-neutral yield-harvesting variants.
  Testing a genuinely different mechanism *family* (directional
  trend-following) was more informative than testing yet another
  basis-yield venue, even though it also came up dead — it rules out an
  entire adjacent strategy category rather than one more instance of the
  same one.
- Leverage does not scale a mechanical trend-following backtest's return
  the way it scales a market-neutral funding trade's return (Thread B): in
  Thread B, leverage just dials capital-efficiency (`L/(L+1)` of a fixed
  rate) with a shrinking liquidation buffer; here, leverage actively
  *destroys* expected return past a low threshold via volatility drag on a
  directionally-risky (unhedged) position, on top of shrinking the
  liquidation buffer the same way. Any future directional-strategy thread
  should expect the same drag effect and should not assume "just add
  leverage" is a free lever, the way some options/DeFi marketing framing
  implies.

---

## Thread K (post-A-F follow-up #9): pairs mean-reversion stat-arb (iteration 21)

**Concept tested:** a third, distinct mechanism family beyond market-neutral
yield harvesting (Threads A-I, funding/basis/options/LP/counterparty flow)
and single-asset directional momentum (Thread J): relative-value pairs
trading. Long the underperforming leg / short the outperforming leg of a
correlated pair (BTC/ETH, BTC/SOL, BTC/AVAX, BTC/LINK, ETH/SOL, ETH/AVAX,
ETH/LINK, SOL/AVAX, SOL/LINK, AVAX/LINK — all 10 combinations of 5 majors)
whenever the log-price-ratio spread's rolling z-score exceeds ±2.0,
exiting when it reverts to within ±0.5 — a mechanical, no-lookahead,
equal-notional market-neutral rule.

**Data pulled, 100% real:** `research/pairs_stat_arb_scan.py` pulls 700
real daily closes (2024-08-04 → 2026-07-05) for BTC/ETH/SOL/AVAX/LINK via
Hyperliquid `candleSnapshot` — the same endpoint used in Threads B/D/J.
Verified perfect timestamp alignment across all 5 coins (0 mismatches in
701 daily bars each) before trusting any cross-coin spread calculation, to
rule out a data-misalignment artifact producing spuriously large
single-day moves.

**Results (30 pair×leverage rows, 20d z-score window, entry 2.0σ/exit
0.5σ):**

| Pair | Lev | Monthly-equiv | Max DD | Worst 1d adverse | Liquidated? |
|---|---|---|---|---|---|
| BTC/SOL | 1x | **+0.82%** (best row) | -27.0% | 10.5% | No |
| SOL/AVAX | 1x | +0.43% | -38.7% | 20.7% | No |
| BTC/ETH | 1x | -3.40% | -59.4% | 10.4% | No |
| ETH/AVAX | 1x | -4.08% | -75.9% | 21.5% | No |
| every pair | 2x-3x | worse than 1x, 6/20 rows liquidated outright | — | — | — |

29 of 30 rows are net-negative or barely positive; every leveraged row is
worse than its own 1x baseline (the same volatility-drag effect found in
Thread J). 0/30 rows clear 5%/month.

**Verdict: DEAD.** Crypto-major price ratios trend rather than mean-revert
over a 20-day window in this 700-day sample — the "fade the divergence"
premise doesn't hold empirically, so this is a real, not merely
under-optimized, negative result. Standing best candidate across all 15
mechanisms now tested (6 named + 9 post-A-F follow-ups) remains XMR
funding basis (Thread B, iteration 7, 2.04%/month), still both the
highest-yield AND lowest-risk option found.

**Learnings:**
- Crypto altcoin ratios (BTC/LINK, ETH/AVAX, etc.) are not reliably
  mean-reverting at a 20-day lookback — they trend for extended periods
  (consistent with "altcoin beta" behavior, where alts amplify BTC/ETH
  moves directionally rather than oscillating around a stable ratio),
  which is why a mechanical z-score-fade rule loses money on 8 of 10
  tested pairs even before leverage or fees are considered.
  Longer-lookback or cointegration-filtered pair selection (only trade
  pairs that pass an actual stationarity test) is an untested refinement,
  but given 14 prior threads all converged on the same sub-bar ceiling,
  it is unlikely to change the outcome qualitatively.
- Before trusting any cross-coin/cross-venue spread calculation, verify
  timestamp alignment explicitly (as done here: 0 mismatches across 5
  coins' 701-bar histories) — this is now the second time in this
  objective (after the STG delisted-market artifact in iteration 15) that
  a plausible-looking anomalous number needed a specific data-integrity
  check before being trusted as real, and doing so before writing up
  results is the correct order of operations regardless of whether the
  result is later confirmed or debunked.
- This is now 9 post-A-F follow-up mechanisms tested (iterations 6, 7, 15,
  16, 17, 18, 19, 20, 21) beyond the 6 originally-named threads, all
  DEAD/NOT VALIDATED — every distinct mechanism *family* accessible with
  free public data (market-neutral yield harvesting in 6 flavors,
  directional momentum, and now relative-value pairs trading) has been
  tested at least once with real data and none beats the standing XMR
  candidate.

---

## Thread L: Hyperliquid public-vault copy-deposit — **NOT VALIDATED (best candidate is a single-manager outlier, not a deployable edge)**

**Script:** `research/hyperliquid_vault_scan.py`

A 4th distinct mechanism family beyond market-neutral yield-harvesting,
directional momentum, and relative-value pairs trading (all of which we ran
ourselves): passively depositing into an existing, already-live Hyperliquid
vault (HLP itself, or a "protocol" vault run by an anonymous third-party
trader). Deposit/withdraw is a single scriptable on-chain action, so this is
still "automatable" per the objective, but the return stream itself is
opaque and controlled by someone else — a genuinely new risk axis (manager/
agency risk) that none of the prior 15 mechanisms carried.

**Method:** pulled the full live Hyperliquid vault universe (real data, no
API key) from `stats-data.hyperliquid.xyz/Mainnet/vaults` — 9,463 vaults ever
created — to get a platform-wide survivorship base rate, then pulled real
per-vault `vaultDetails` (daily-resampled account-value + cumulative-PnL
history since inception) for all 36 currently-open vaults with TVL ≥
$500k. For each, computed the live "apr" snapshot field, the last-30-day
return, a flow-blind "sustained" all-time monthly return (all-time PnL ÷
average all-time equity ÷ months elapsed, which ignores deposit/withdrawal
noise in the raw TVL curve), and the max PnL-based drawdown expressed as %
of the vault's contemporaneous equity.

**Platform-wide survivorship base rate (real, n=9,463 vaults ever created):**

| Metric | Value |
|---|---|
| Total vaults ever created | 9,463 |
| Already closed | 6,275 (66.3%) |
| Of closed: ended with negative all-time cumulative PnL | 3,476 (55.4% of closed) |
| Of closed: ended with positive all-time cumulative PnL | 1,433 (22.8% of closed) |
| Of closed: flat/dust (never real capital) | 1,366 |

Two out of every three vaults ever launched on Hyperliquid are already
closed, and more than half of those closed at a loss — this is the base
rate against which any single "hot" vault must be judged.

**Top open vaults by TVL ≥ $500k, live-snapshot APR vs. real sustained history (2026-07-05):**

| Vault | TVL | Live APR | Last 30d | Days live | Sustained %/mo | Max PnL drawdown (% of eq) | Leader skin-in-game |
|---|---|---|---|---|---|---|---|
| Hyperrr | $1.75M | 241%/yr | +40.3% | 556 | 9.4%/mo | **-509%** | 20.7% |
| [Systemic Strategies] HyperGrowth | $12.8M | 14%/yr | +1.2% | 304 | **9.1%/mo** | -67.9% | 5.7% |
| Long HYPE & BTC \| Short Garbage | $2.75M | 46%/yr | +3.8% | 451 | 8.5%/mo | -190% | 46.6% |
| Satori Quantum HF Vault | $0.52M | 111%/yr | +9.2% | 486 | 4.1%/mo | -47.7% | 12.3% |
| Growi HF | $9.24M | 120%/yr | +10.3% | 724 | 2.7%/mo | -12.0% | 5.0% |
| drkmttr | $5.89M | 234%/yr | +25.7% | 206 | 1.0%/mo | **-168%** | 40.4% |
| Hyperliquidity Provider (HLP) | $261M | -0.02%/yr | -0.0% | 1,151 | 1.6%/mo | -6.9% | 0.1% |

(Full 36-vault table in `data/hyperliquid_vault_scan_results.json`.)

Every vault whose **live** APR looks spectacular (Hyperrr 241%/yr, drkmttr
234%/yr, AILAB TEST ULTRA 344%/yr, Hyperliquidity Trader 114%/yr) turns out,
once the full since-inception history is examined, to have already
suffered a PnL-based drawdown of -168% to -3,107% of its own contemporaneous
equity at some point — i.e. it would have wiped out (and in a real fund,
liquidated/closed) any depositor who was in during that window, multiple
times over. This is the same live-snapshot-vs-sustained-mean spike trap
already found for funding rates (iteration 7) and vault-token "real yield"
(iterations 18/19), now confirmed a third time in a structurally different
data source (discretionary trader PnL, not a market rate).

**drkmttr case study** (deep-dived because it had the single highest
snapshot APR among vaults with real TVL and >100 days of history): its
all-time cumulative PnL is only **+$290k** on capital that has ranged
$0.6M-$6.7M over 7 months — i.e. essentially break-even after 7 months —
despite a current 30-day snapshot of +25.7%. The equity curve shows a
-$2.6M cumulative-PnL trough on 2026-06-06 that was worse than -100% of
the vault's equity at that moment, followed by a sharp recent recovery that
is entirely responsible for the attractive live APR figure.

**The one candidate that survives scrutiny, with important caveats:**
[Systemic Strategies] HyperGrowth ($12.8M TVL, 100 followers, live since
2025-09) had a rocky first ~5 months (drawdowns to -68% of equity,
Sep 2025-Jan 2026) but has shown a materially smoother, consistently
compounding equity curve since 2026-02 — cumulative PnL grew from ~$1.3M
to ~$6.9M over the following ~5.3 months with drawdowns confined to
single digits (worst -18.7%), which on its own annualizes to roughly
**10-12%/month** over that recent window. This is the single best
sustained-return number found in this entire objective, across all 16
mechanisms tested.

**Why this is NOT written up as a validated deploy target:**
- It is one anonymous discretionary manager's track record, not a
  mechanical/backtestable edge — nothing prevents the strategy, leverage,
  or manager discipline from reverting to the -68%-drawdown regime seen in
  its own first 5 months.
- The leader's own skin-in-the-game is only 5.7% of the vault's equity —
  low alignment between manager risk and depositor risk.
- The "good" regime is only ~5 months old; the platform-wide base rate
  (66% of all vaults eventually close, 55% of those at a loss) means the
  prior probability that any single currently-hot vault is still open and
  profitable a year from now is well under 50%.
- A 10% depositor-side fee (leaderCommission) applies on profits, and
  vault deposits carry a real, per-follower lockup (observed ~30 days from
  each entry in this vault's own follower list) that prevents fast exit if
  the drawdown regime returns.
- This risk is fundamentally different in kind from every prior thread in
  this objective (market/basis/volatility risk, which can be bounded from
  price history) — it is manager/agency risk, which cannot be bounded from
  historical data at all; a manager can always change behavior, take on
  more risk, or the vault can simply be a slow rug.

**Verdict: NOT VALIDATED.** No open, real-TVL, >100-day-history Hyperliquid
vault combines a sustained (not live-snapshot) return above 5%/month with a
drawdown history that would count as "survivable" by this objective's own
standard (a strategy that blows up even 1-in-10 periods has negative EV).
The best candidate (Systemic Strategies HyperGrowth, ~10-12%/month over its
recent 5-month regime) is disclosed here as the single highest-yield
number found across all 16 tested mechanisms, but it fails the
"quantified drawdown, not hidden" test on its own first-5-month history,
and manager/agency risk is not something backtesting can bound the way it
can for the market-neutral trades tested elsewhere in this objective. Not
promoted to `DEPLOY_THIS.md`.

**Learnings:**
- Hyperliquid's `stats-data.hyperliquid.xyz/Mainnet/vaults` endpoint (no
  API key) returns the FULL universe of every vault ever created, including
  closed ones with their final PnL — this is a rare case in this objective
  of a venue voluntarily exposing its own base-rate failure statistics
  (66% closed, 55% of those at a loss), which should be treated as a strong
  prior before trusting any single live "hot vault" result.
- `vaultDetails`'s `accountValueHistory` (raw TVL) is contaminated by
  deposit/withdrawal flows and cannot be used directly to compute a
  return-on-capital drawdown; `pnlHistory` (cumulative dollar trading PnL,
  which flows do not affect) divided by contemporaneous equity is the
  correct flow-blind way to measure a vault's real drawdown, the same
  distinction that mattered for JLP's vault-token APY-vs-total-return
  decomposition in iteration 18.
- This is the 10th post-A-F follow-up mechanism tested (after iterations 6,
  7, 15, 16, 17, 18, 19, 20, 21) and the first to surface a single number
  that nominally clears the 10%/month target — but it is also the first
  thread where the disqualifying risk is un-backtestable manager/agency
  risk rather than a quantifiable market/volatility risk, which is why it
  is reported as a disclosed outlier rather than promoted to a validated
  deploy target. Standing best *validated* (mechanical, backtestable)
  candidate remains XMR funding basis (Thread B, iteration 7, 2.04%/month).
