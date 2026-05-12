# Polybot Lite Commands

## 1) Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests websocket-client python-dotenv playwright
playwright install chromium
```

## 2) One-shot market prices (all brackets)
```bash
python3 run.py prices
```

## 3) One-shot tweet stream state (SSE + fallbacks)
```bash
python3 run.py tweets
```

## 4) One-shot SSE/XTracker health check
```bash
python3 run.py health
```

## 5) One-shot model prediction
```bash
python3 run.py model
```

## 5a) Data coverage / gap report (2009 -> now)
```bash
python3 run.py coverage --cache data/tweets_all.parquet --start 2009-01-01T00:00:00Z --out-dir data/reports/coverage
```
Outputs:
- `data/reports/coverage/summary.json`
- `data/reports/coverage/year_counts.csv`
- `data/reports/coverage/month_counts.csv`
- `data/reports/coverage/missing_months.csv`
- `data/reports/coverage/source_counts.csv`

## 5b) One-shot paper summary (P&L, wins/losses, recent closes)
```bash
python3 run.py summary --log paper_trade_events.jsonl --last 40
```

## 5c) Build model-ready feature tables (per_tweet / daily / weekly)
```bash
python3 run.py features --out-dir data/features
```

## 6) Live terminal logger (recommended)
```bash
python3 run.py live --tick-s 3
```

## 7) Paper-trade terminal (new)
```bash
python3 run.py paper --tick-s 8
```
Defaults now keep JSONL files bounded:
- `paper_trade_events.jsonl`: capped to latest 400 lines (about 0.5MB max)
- `paper_picks_snapshot.jsonl`: overwrite latest snapshot each tick (no growth)
- SSE JSONL logs: capped by env vars (`POLYBOT_JSONL_MAX_BYTES`, `POLYBOT_JSONL_KEEP_LINES`)

### Optional cost controls in paper mode
```bash
python3 run.py paper --tick-s 8 --fee-category mentions --slippage-rate 0.005 --uncertainty 0.05
```
Polymarket taker fees are modeled as `shares * feeRate * p * (1 - p)`.
Use `--fee-rate` only when you intentionally want to override the documented
category table.

### Optional safety controls (recommended for long runs)
```bash
python3 run.py paper --tick-s 8 --min-entry-price 0.02 --min-trade-cost 25 --low-price-threshold 0.05 --low-price-max-cost 25 --stop-loss-cooldown-s 7200 --require-twitter-ok-for-entries
```

If you intentionally want entries even while the Twitter SSE stream is down:
```bash
python3 run.py paper --tick-s 8 --allow-entries-without-twitter
```

### Optional event-universe controls (auto add/remove tweet markets)
```bash
POLYBOT_EVENT_REFRESH_S=60 POLYBOT_EVENT_MAX_EVENTS=12 python3 run.py paper --tick-s 8
```

### Optional tweet-signal fallbacks (use logged-in account session)
```bash
POLYBOT_ENABLE_SEARCH_POLL=1 POLYBOT_SEARCH_POLL_MS=1500 POLYBOT_ENABLE_NOTIFICATIONS_POLL=1 POLYBOT_NOTIFICATIONS_POLL_MS=2500 python3 run.py paper --tick-s 8
```

### Hedge controls (enabled by default)
```bash
python3 run.py paper --hedge-ratio 0.35 --max-hedges-per-primary 1 --hedge-min-true-edge 0.02 --max-event-exposure-pct 0.45
```

## 7a) Parameter sweep / walk-forward research
```bash
python3 -m research.parameter_sweep --tweets data/tweets_all.csv --out-dir data/reports/sweeps --stage-a 36 --stage-b 18 --finalists 6
```

Fast smoke run:
```bash
python3 -m research.parameter_sweep --quick --tweets data/tweets_all.csv --out-dir data/reports/sweeps
```

Outputs per run directory:
- `leaderboard_dev_all_competitors.csv`
- `leaderboard_stage_c_holdout.csv`
- `top10_configs.json`
- `top3_attribution.json`
- `worst10_failure_analysis.json`
- `winner_sensitivity.csv`
- `finalist_configs/*.json`

## 7b) Live real-data competitor arena (48h)
Runs every competitor from the latest sweep leaderboard side-by-side on real
Polymarket/Twitter/XTracker data. No synthetic prices are used.
```bash
python3 -m research.live_competitor_test --duration-h 48 --tick-s 30 --report-s 300 --out-dir data/reports/live_competitor_tests
```

Run the exact 2026-05-02 sweep competitors:
```bash
python3 -m research.live_competitor_test --duration-h 48 --tick-s 30 --report-s 300 --leaderboard-csv data/reports/sweeps/20260502_104223/leaderboard_dev_all_competitors.csv --out-dir data/reports/live_competitor_tests
```

Strict real-BBO run, no synthetic midpoint fallback:
```bash
caffeinate -dims python3 -u -m research.live_competitor_test \
  --duration-h 48 \
  --tick-s 10 \
  --report-s 300 \
  --input-archive-s 10 \
  --leaderboard-csv data/reports/sweeps/20260502_165950/leaderboard_dev_all_competitors.csv \
  --out-dir data/reports/live_competitor_tests
```

### Compact live status
```bash
python3 -m research.live_status
```

Live refreshing view:
```bash
python3 -m research.live_status --watch-s 60 --top 10
```

Plot the current live arena PnL, drawdowns, and sweep summary:
```bash
python3 -m research.plot_competitor_pnl --run-dir data/reports/live_competitor_tests/20260502_110010_real_48h
```

Diagnose Polymarket feed/archive latency and price-row changes:
```bash
python3 -m research.feed_latency_report --run-dir data/reports/live_competitor_tests/20260502_170356_real_48h
```

Outputs per run directory:
- `run_manifest.json`
- `real_input_ticks.jsonl.gz` (real market/tweet/health snapshots)
- `portfolio_snapshots.jsonl`
- `leaderboard_latest.csv`
- `leaderboard_history.jsonl`
- `daily_metrics_latest.json`
- `daily_metrics_history.jsonl`
- `loop_metrics.jsonl`
- `final_report.json`
- `competitors/<id>/trades.jsonl`
- `plots/*.png`
- `plots/latest_summary.csv`
- `plots/feed_latency_summary.csv`
- `plots/feed_tick_gap_and_price_changes.png`
- `plots/arena_loop_elapsed.png`

### Logs for "all picks + invested amount"
```bash
python3 run.py paper --log paper_trade_events.jsonl --picks-log paper_picks_snapshot.jsonl
```

### If you want pick snapshots appended (instead of overwrite)
```bash
python3 run.py paper --picks-append --picks-max-lines 300 --picks-max-bytes 1000000
```

## 8) Direct module runs (optional)
```bash
python3 prices.py
python3 tweets.py
python3 xtracker_health.py
python3 terminal_logger.py --tick-s 3
python3 paper_trade_terminal.py --tick-s 3
python3 -m ingestion.collect --gist-archive --refresh
python3 -m ingestion.collect --kaggle --twscrape --since 2022-01-01 --until 2026-05-01
X_BEARER_TOKEN=... python3 -m ingestion.collect --x-full-archive --x-start-time 2009-01-01T00:00:00Z --x-query "from:elonmusk"
python3 -m ingestion.coverage_report --cache data/tweets_all.parquet --start 2009-01-01T00:00:00Z
```

## Notes
- SSE depends on `TWITTER_COOKIE`, `TWITTER_BEARER`, and `TWITTER_CSRF` in `.env`.
- If SSE is stale, the system automatically keeps GraphQL/XTracker/RSS fallback paths visible.
- Terminal events are written to `lite_terminal_events.jsonl`.
- Paper trades are written to `paper_trade_events.jsonl`.
- Tweet dataset rows are written to `tweet_dataset.jsonl` for sentiment + density backbone.


python3 -m research.plot_competitor_pnl --run-dir data/reports/live_competitor_tests/20260502_174718_real_48h --out-dir notebooks/png

python3 -m research.plot_param_correlations --out-dir notebooks/png

