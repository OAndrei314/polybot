# Polymarket smart-money / insider-trading detection pipeline

Identifies and tracks wallets with above-market win rates on Polymarket,
then backtests copy-trading them at fractional size.

## Quick state check

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -like "polybot*" } |
  Select-Object TaskName, State, LastRunTime, NextRunTime
```

Three Windows scheduled tasks should be running:

| Task | Frequency | Duration | What it does |
|---|---|---|---|
| `polybot_wallet_discovery` | every 4 h | 8 days | Walks recent-trades feed, accumulates active wallets to `wallets.jsonl.gz`. |
| `polybot_copy_tracker` | every 30 min | 30 days | Polls the 2 target wallets, records new BUYs, scores against eventual resolution. |
| `polybot_weekly_aggregate` | weekly | 56 days | Refreshes top 300 wallet histories, reruns aggregation + copy backtest. |

Live progress: tail `logs/copy_tracker.log` and `logs/wallet_discovery_cron.log`.

## Manual one-off scripts

```bash
# Discover currently-active wallets (one tick)
python -m research.polymarket_alpha.discover_wallets --pages 50

# Pull resolved markets (last 6 months)
python -m research.polymarket_alpha.fetch_markets --max-markets 2000

# Pull full history for top wallets (3500-trade cap per wallet)
python -m research.polymarket_alpha.fetch_wallet_histories --top 100

# Aggregate per-wallet stats vs resolved markets
python -m research.polymarket_alpha.aggregate

# Phase 2: flag suspicious "big late win" pattern
python -m research.polymarket_alpha.detect_patterns

# Investigate Iran cluster manually
python -m research.polymarket_alpha.investigate_iran

# Backtest copy-trading the top wallets at 1/10 size
python -m research.polymarket_alpha.copy_backtest

# Live tracker tick (also runs every 30 min via cron)
python -m research.polymarket_alpha.live_copy_tracker
```

## Polygonscan setup (optional, for missing-history estimation)

The Polymarket data-api hard-caps pagination at offset 3500 across every
endpoint. For high-volume wallets we can't see beyond their most recent
~3500 trades. To estimate how much we're missing, get a free Polygonscan
API key.

1. Sign up at https://polygonscan.com/myapikey (30 sec, no card).
2. Create an API key (free tier: 5 req/sec, 100k req/day).
3. Set the env var permanently:

```powershell
setx POLYGONSCAN_API_KEY "YOUR_KEY_HERE"
```

(Open a new terminal after `setx`.) Then:

```bash
python -m research.polymarket_alpha.estimate_missing_history
```

Without the key, the script silently reports "?" for each wallet's
on-chain tx count.

## Pre-registered live copy-trade test

See `data/experiments/copy_trade_live_prereg.md` — locked decision rule
for whether the in-sample backtest (+$24,660 at 1/10 size, 38%
efficiency) reproduces on out-of-sample bets the targets place after the
prereg lock date.

Read the rolling tally any time:

```powershell
Get-Content data\polymarket_alpha\copy_tracker\summary.json
```

## Honest limitations

- **3500-trade cap** on wallet history limits high-volume wallets to recent
  tail. Full history requires Polygon RPC + CTF event decoding (~2-3
  days of additional work).
- **No real-time fills** — the data-api is poll-based with seconds-to-
  minutes latency. Live copying with real money would need WebSocket
  subscription + sub-second execution to capture sharp wallets before the
  market moves.
- **Geo blocked** — even with a validated edge, Polymarket is blocked
  from Germany. The pipeline produces research, not money.
- **Sample of 100-300 wallets** is small; the discovery cron mitigates
  over time but doesn't cover the long tail.
