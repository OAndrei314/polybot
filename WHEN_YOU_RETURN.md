# 96-hour experiment — what to do when you return

This file is the one-page checklist for the run that **pivoted at 2026-05-14 11:39 UTC** and ends **~2026-05-18 10:39 UTC**.

> **Pivot note:** the original v1 run (10:34) was too conservative — only 10/100 strategies traded in 53 min. The pool was regenerated with `entry_edge` and `spread_floor_multiplier` ranges shifted into the zone where real opportunities exist (true_edge 0.10-0.18). v2 jumped to 50 trading competitors in 30s. Arena PID **5764**, run dir `20260514_113946_real_48h`. Pre-pivot data preserved at `20260514_103411_real_48h` for comparison.

## Quick health check (60 seconds)

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Select-Object ProcessId, CommandLine | Format-List
```

You should still see:
- `live_competitor_test --duration-h 96` (the arena)
- `snapshot_96h --interval-min 120` (the periodic snapshot writer)

If either is missing, see "If something died" below.

```powershell
Get-Content logs\arena_96h_stdout.log -Tail 5
Get-Content logs\snapshot_96h_stdout.log -Tail 5
```

Arena should be ticking every ~60s with non-zero P&L numbers. Snapshot watcher should have printed ~48 `snapshot #N` lines by hour 96.

## Generate the final report (1 command)

```powershell
.\.venv\Scripts\python.exe -m research.analyze_96h
```

Outputs:
- `data\experiments\96h_report\REPORT.md` — **read this first**
- `data\experiments\96h_report\winners.csv` — top 30 competitors with their full params
- `data\experiments\96h_report\calibration.csv` — predicted prob vs realized win rate
- `data\experiments\96h_report\timeseries.csv` — leaderboard evolution over time
- `notebooks\png\96h_pnl_trajectory.png` — P&L over time (min-max band + median)
- `notebooks\png\96h_calibration.png` — predicted vs realized scatter

Also refresh the standard plot suite:
```powershell
.\.venv\Scripts\python.exe -m research.refresh_plots
```

## What the experiment is testing

100 competitors, each with different model parameters, all running on the **same** real-time Polymarket + X data. The arena leaderboard ranks them by realized P&L over 96 hours. The pool is:

- **4 anchors** — hand-crafted "paradigm" configs: `anchor_balanced`, `anchor_selective`, `anchor_quick_profit`, `anchor_conservative`
- **96 LHS samples** — Latin Hypercube Sampling across 12 dimensions:

| dimension | range | what it controls |
|---|---|---|
| `entry_edge` | 0.06 – 0.22 | how much edge required to enter |
| `take_profit` | 0.04 – 0.18 | exit at this much profit |
| `stop_loss` | 0.15 – 0.40 | exit at this much loss |
| `nb_phi` | 5 – 55 | dispersion floor (clamped to empirical) |
| `rate_prior_strength_h` | 10 – 60 | how slow to update on observed rates |
| `hawkes_alpha` | 0.05 – 0.38 | tweet excitation strength |
| `uncertainty` | 0.02 – 0.10 | flat probability haircut |
| `risk_per_trade_pct` | 0.02 – 0.10 | bankroll fraction per trade |
| `spread_floor_multiplier` | 0.4 – 1.6 | slippage padding |
| `signal_flip_min_edge_gap` | 0.03 – 0.12 | when to flip sides |
| `max_event_exposure_pct` | 0.20 – 0.55 | bankroll cap per event |
| `position_size_disagreement_penalty` | 0.5 – 2.0 | size shrink when signals disagree |

All competitors share these baked-in code patches:
- `_phi_for_window` clamps phi to empirical floor (no more near-Poisson overconfidence)
- `edge_decay` exit guarded by `hours_left > 6` and `pnl_pct < 0` (hold-to-resolution)
- `_try_entries` rejects NO+>72h, <15c+>24h, and >65c brackets (dead-bucket filter)

## What "success" looks like in the report

You're looking for:

1. **≥10 profitable competitors out of 100** (the previous unpatched run had 2/100)
2. **Median P&L > 0** (was −$22 in the unpatched run)
3. **Calibration table** showing predicted ≈ realized in each decile (was −60pp off before)
4. **Parameter correlations** that reveal *which* knob settings matter most for profit

If those four things look healthy, take the top 3-5 winners' configs and start a new arena with just those, perturbed, for the next iteration.

## If something died

```powershell
# Resurrect the arena (use the existing run dir, don't lose state)
$env:POLYBOT_FAST_GQL="1"; $env:POLYBOT_ENABLE_SSE_POOL="0"
$env:POLYBOT_GQL_MIN_INTERVAL_S="6.5"; $env:POLYBOT_GQL_JITTER_S="1.5"
$env:POLYBOT_GQL_RATE_SLACK_S="0.5"; $env:POLYBOT_XTRACKER_TTL_S="20"

# Compute remaining hours from when you returned vs the 10:34 UTC start time
$remaining_h = 96 - ((Get-Date).ToUniversalTime() - (Get-Date "2026-05-14T10:34:00Z").ToUniversalTime()).TotalHours

Start-Process -FilePath ".\.venv\Scripts\python.exe" `
  -ArgumentList @("-u","-m","research.live_competitor_test",
    "--duration-h",[math]::Max(1, [int]$remaining_h),
    "--tick-s","1","--report-s","60","--input-archive-s","5","--health-poll-s","30",
    "--max-competitors","100","--configs-glob","data/experiments/96h_pool/*.json",
    "--out-dir","data/reports/live_competitor_tests") `
  -WindowStyle Hidden -RedirectStandardOutput "logs\arena_96h_stdout.log" `
  -RedirectStandardError "logs\arena_96h_stderr.log"
```

Even if the arena died early, the snapshots and the partial trade log are still usable — `analyze_96h` works on however much data exists.

## After the analysis — what next?

Three plausible follow-ups, in order:

1. **Iterate** — take the top 5 by P&L, perturb their params ±20%, generate a new 100-pool, run another 96h arena. Repeat until P&L plateaus.
2. **Validate calibration** — run only the top-1 strategy for a week and measure realized vs predicted win rate per decile. This proves the model is actually right, not just lucky.
3. **Go live (small)** — once a strategy is consistently positive in paper and well-calibrated, run it on Polymarket with $50-100 of real money for one week. Real execution path needs building first.

## File index

- Pool generator:  `research\generate_96h_pool.py`
- Snapshot watcher: `research\snapshot_96h.py`
- Final analyzer:  `research\analyze_96h.py`
- Pool configs:    `data\experiments\96h_pool\*.json` (100 files)
- Periodic snapshots: `data\experiments\96h_snapshots\snapshot_*.json` (~48 files at end)
- Arena run dir:   `data\reports\live_competitor_tests\20260514_103411_real_48h\` *(label says 48h but it's actually a 96h run)*
- Logs:            `logs\arena_96h_*.log`, `logs\snapshot_96h_*.log`

## One last thing

Power settings already have sleep disabled (`powercfg /change standby-timeout-ac 0`). To re-enable after the experiment:
```powershell
powercfg /change standby-timeout-ac 30
powercfg /change standby-timeout-dc 15
```
