# Deploy This: Honest Best-Candidate Verdict (2026-07-04)

## Bottom line

**No thread cleared the 5%/month floor, let alone the 10%/month target.** All six
threads named in this objective's priority list (A-F) were tested against real,
live-pulled data and are DEAD, NOT VALIDATED, or INFEASIBLE. This document exists
because the objective explicitly instructs: *"If nothing clears 5%/month, write your
best candidate with honest numbers and stop."* This is that write-up — it is a
disclosure of the best available risk-adjusted option, **not** a strategy that meets
the bar. Do not read the presence of this file as validation.

---

## Full thread scorecard (see `research/FINDINGS_BEYOND_POLYMARKET.md` for full detail)

| Thread | Strategy | Verdict | Real net yield found |
|---|---|---|---|
| A | Deribit/Bybit short-put premium selling | DEAD | Net **negative** (fat-tail weeks erase 20+ weeks of premium) |
| B | Hyperliquid funding basis, leveraged | DEAD (vs. 5%/mo bar) | Best safe case (full 231-coin universe scan, iter. 7): XMR ~**2.04%/month** (24.5%/year); original 3-coin sample: ~4.6-6.2%/year (~0.4-0.5%/month) |
| C | Uniswap V3 / Orca concentrated LP | DEAD (stable) / NOT VALIDATED (volatile) | Stable: 0.03-0.5%/month. Volatile: ~3.3%/month fee ceiling, IL from an ordinary month's swing can match/exceed it |
| D | Hyperliquid mark-vs-oracle CEX-DEX arb | DEAD | Gap never exceeds 0.064%, need >0.3% to be tradable |
| E | Kalshi/Polymarket via VPN | DEAD/INFEASIBLE | N/A — legal/regulatory risk, not a yield question |
| F | Solana liquidation bots (Drift/marginfi) | NOT VALIDATED (hardened, iter. 17) | Drift (primary venue) still offline; **direct on-chain measurement of marginfi mainnet (iter. 17)** found liquidations in ≤1 of ~340 sampled real transactions (rare), while 93% of the program's real traffic is flash-loan-arb bots with a 96% real failure rate — concrete on-chain proof the adjacent niche is already bot-saturated |

---

## Best candidate: Hyperliquid perp funding-basis (Thread B), unleveraged or lightly leveraged

This is the only thread in the full search with **clean, quantified, positive
expected value and bounded, disclosed risk** — everything else is either negative EV,
fee-ceiling-bound with offsetting IL, arbitraged away to nothing, blocked on legal
grounds, or unquantifiable with real data. It does not hit this objective's bar; it is
being surfaced because the objective requires disclosing the best candidate found,
not withholding one because it's underwhelming.

**Mechanism:** Hold spot (or a delta-neutral equivalent) long on a coin with
persistently positive perp funding, short the equivalent notional on Hyperliquid
perps. Collects the funding rate as yield; price risk is hedged out by construction
(long spot + short perp = flat delta), with basis-tracking/liquidation risk being the
residual exposure on the short leg specifically.

**Real numbers (from Thread B, iteration 2, 90 days of real hourly `fundingHistory`
and `candleSnapshot` data on kBONK/ENA/PUMP; broadened in iteration 7's addendum to
scan Hyperliquid's full ~231-coin universe):**
- Unleveraged (1x), original 3-coin sample (kBONK/ENA/PUMP): ~5-7% net APR
  (~0.4-0.6%/month).
- Leveraged to the maximum level that survived the full 90-day real price history
  without a liquidation breach (2-3x, coin-dependent), original 3-coin sample:
  ~4.6-6.2%/year net (~0.4-0.5%/month) — leverage barely moves the needle because
  capital efficiency scales as L/(L+1) while the liquidation buffer shrinks as 1/L.
- **Best honest candidate after scanning the full coin universe (iteration 7):**
  XMR, at 3x leverage sized to its own real 90-day worst-case price moves, nets
  **~2.04%/month** (24.5%/year) on a *sustained* 90-day-mean funding rate (32.7%/yr,
  positive in 98% of hourly prints) — this is ~4-5x better than the original 3-coin
  figure and is now the single best number found across every thread in this
  objective. A live-snapshot scan found much higher-looking coins (GRASS at
  168%/yr, ZRO at 70%/yr), but 90-day history showed both were momentary funding
  spikes (11x and 4x their sustained mean, respectively) — not rates a monthly
  strategy could count on. Even at this improved number, **2.04%/month still does
  not clear the 5%/month floor.**

**Risk (quantified, not hidden):**
- Funding rate can flip negative (pay instead of receive) if market sentiment
  reverses — this was not stress-tested beyond the observed 90-day window's sign.
- The short leg's liquidation threshold is a hard function of leverage: at 3x, a
  ~16.7% adverse move against the short (before maintenance-margin buffer) triggers
  liquidation; the 90-day data showed this survived, but 90 days is not a guarantee
  against a larger future move (e.g., a de-listing event, a de-peg, or an
  extreme single-coin crash/rally on a thin altcoin like kBONK/ENA/PUMP).
- Spot leg and perp leg typically live in different wallets/venues — during a fast
  move, capital cannot always be transferred between them fast enough to top up
  margin, which is the real mechanism behind a liquidation, not basis divergence
  itself (Hyperliquid's funding mechanism keeps mark≈oracle within single-digit bps
  per Thread D, so basis-divergence risk specifically is negligible — the risk is
  underlying price volatility of the shorted coin).

**Automation:** fully automatable in Python today — Hyperliquid has a complete
REST/WebSocket API, no KYC gate, and is accessible from Romania on-chain. This is
the only thread where "build it in Python and run it unattended" is both true and
uncontested by the data.

**Geo-accessibility:** yes, on-chain, no VPN or identity misrepresentation needed —
unlike Threads E and F, this carries no legal/regulatory tail-risk for a Romanian
resident.

**Capital efficiency:** works at the $500-5,000 test range named in this objective's
brief; scales with more capital without materially changing the yield (it's not a
fixed-size opportunity like an arb gap or a liquidation bounty).

---

## What this means for the user

At ~0.4-2.0%/month (best case: XMR funding basis, iteration 7), this does not
satisfy "10%+/month" or even the 5%/month floor — it satisfies "positive, real,
quantified, low-blowup-risk yield," which is a different (and much more modest)
thing. Sustained 10%/month without proportional risk does not exist in any of the
six mechanisms this objective named, tested against real market data as of
2026-07-04, nor did broadening the funding-basis coin search to Hyperliquid's full
~231-coin universe (iteration 7) find a sustained (non-spike) rate that clears
5%/month. If the user wants to pursue yield beyond this level, it requires either
(a) accepting one of the higher-variance, unquantified-EV paths already marked dead
here (fat-tailed options selling, MEV latency races) with eyes open to the disclosed
blowup risk, or (b) a fundamentally different strategy class not on this
objective's list.

**Post-A-F follow-up mechanisms tested (all still DEAD, see `FINDINGS_BEYOND_POLYMARKET.md`):**
options credit spreads + IV-rank gating (iter. 6), full-universe funding scan → XMR
(iter. 7), cross-exchange HL-vs-Bybit perp-perp funding differential → best real
candidate STRK 1.25%/month (iter. 15), and Deribit dated-futures cash-and-carry
basis on BTC/ETH → best 0.33%/month even at the farthest (~1yr) expiry, with 90-day
history confirming today's thin contango is not a temporary dip (iter. 16). None
beat the standing XMR figure above.

**Thread F re-test (iteration 17):** the one thread that was previously
"NOT VALIDATED" only because every documented data source was blocked (Drift API
403, S3 bucket 403, Dune 404) has now been measured directly on Solana mainnet via
public RPC (`research/solana_liquidation_scan.py`, no API key needed). Real
liquidations are rare (≤1 of ~340 sampled marginfi-program transactions), while
93% of that program's real transaction volume is flash-loan-arbitrage bots that
fail 96% of the time — direct on-chain evidence that even the adjacent, more
frequent opportunity on this exact program is already fiercely contested by
automated competitors. This converts Thread F from "blocked, unquantifiable" to
"measured, and the measurement itself argues against a new entrant finding an
open lane." Still does not change the standing best candidate (XMR, 2.04%/month).

**Thread H (iteration 18): Jupiter Perpetuals (JLP) counterparty-vault "real
yield" token.** A genuinely new mechanism (LP takes the other side of leveraged
trader P&L, not funding/basis/options/AMM-fees) marketed at "30-60% APY." Real
180-day CoinGecko price data (`research/jlp_perps_vault_scan.py`) shows a real
USD holder actually **lost 23.5%** over the last 6 months — driven almost
entirely by JLP's ~64% embedded long exposure to SOL/BTC/ETH (each -33% to -46%
over the window), not a mechanism failure. Netting out that beta against a
static-weight benchmark isolates the actual counterparty/fee yield at a
consistent **~0.5-1.4%/month** across the 30/90/180-day windows — sub-bar, and
below the standing XMR candidate. A beta-hedged variant is theoretically
possible but the perp-funding cost of the hedge legs would likely consume the
already-thin excess, so it wasn't built. Key general lesson: any "real yield"
vault token's advertised APY is a fee-generation-rate figure, not a real
holder's total-return figure, and the two can diverge by 20+ points depending
on whether the underlying basket had a good or bad run — always decompose
before trusting a headline APY on this class of product. Standing best
candidate (XMR, 2.04%/month) unchanged.

**Thread I (iteration 19): Ethena sUSDe multi-venue delta-neutral basis
yield.** A diversified, professionally-run version of the same basis-yield
mechanism family (Thread B/G), but harvested across many CEXs at once plus
collateral staking yield, at $1.6B+ TVL — tests whether buying someone else's
already-diversified basis book beats our own single-venue findings. Real full
history since 2024-02 (`research/ethena_susde_scan.py`, DeFiLlama, 871 daily
samples) shows the eye-catching 24-55%/yr readings were confined to the
2024-02→06 launch/incentive window; every month since 2025-01 (18 straight
months) has been steady at 3.5-5%/yr (~0.3-0.4%/month) — below both the
5%/month floor and the standing XMR candidate. Loop-leverage via Aave doesn't
help either: the USDe borrow-cost floor (4.93%/yr, proxied by Aave's own USDe
supply APY) already exceeds the 3.82%/yr base yield being levered, so every
loop iteration is net-negative (5x leverage models to **-0.62%/yr**). This is
a third independent data source (after HL funding history and Deribit's
forward curve) confirming the same regime fact from a different angle:
crypto-market-wide delta-neutral basis yield is thin almost everywhere right
now, not just on the specific venues tested earlier. Standing best candidate
(XMR, 2.04%/month) unchanged; this is now 7 post-A-F mechanisms tested, all
dead.
