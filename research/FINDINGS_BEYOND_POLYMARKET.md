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
