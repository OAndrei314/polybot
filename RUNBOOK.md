# Polybot Runbook

## 1) Arena Bot (live_competitor_test)

### Start
```bash
caffeinate -dims python3 -u -m research.live_competitor_test \
  --leaderboard-csv data/reports/sweeps/20260502_165950/leaderboard_smart_variants.csv \
  --tick-s 10 \
  --report-s 300 \
  --out-dir data/reports/live_competitor_tests \
  > arena.log 2>&1 &
echo "ARENA_PID=$!"
```

### Stop
```bash
pkill -f "research.live_competitor_test"
```

### Check status (live refresh)
```bash
python3 -m research.live_status --watch-s 60 --top 10
```

### Check status (one-shot, all competitors)
```bash
python3 -m research.live_status --top 100
```

### Generate plots (replace run dir with latest)
```bash
python3 -m research.plot_competitor_pnl \
  --run-dir data/reports/live_competitor_tests/20260504_204650_real_48h \
  --out-dir notebooks/png
```

#### Find latest run dir automatically
```bash
LATEST=$(ls -t data/reports/live_competitor_tests/ | head -1)
python3 -m research.plot_competitor_pnl \
  --run-dir data/reports/live_competitor_tests/$LATEST \
  --out-dir notebooks/png
```

---

## 2) Latency Benchmark (signal speed monitor)

### Start
```bash
nohup python3 -u run.py latency-bench \
  --status-s 60 \
  --gql-interval-s 30 \
  > signal_latency_console.log 2>&1 &
echo "BENCH_PID=$!"
```

### Stop
```bash
pkill -f "run.py latency-bench"
```

### Check live state
```bash
cat data/signal_latency/state.json
```

### Generate plots
```bash
python3 -m research.plot_signal_latency
# outputs to data/signal_latency/plots/
#   latency_hist.png
#   per_tweet_scatter.png
#   first_to_detect.png
#   latency_summary.csv
```

---

## 3) Useful extras

### Paper trade P&L summary
```bash
python3 run.py summary --log paper_trade_events.jsonl --last 40
```

### One-shot prices check
```bash
python3 run.py prices
```

### One-shot tweet stream check
```bash
python3 run.py tweets
```
