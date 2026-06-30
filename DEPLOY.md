# Deploying the v5 paper arena to a Linux VPS

This runs the **v5 paper-trading arena** uninterrupted for a multi-week
validation. It is a **paper** run:

- It reads *public* Polymarket market data and the Elon-tweet feed.
- It trades on an in-memory ledger — **no real orders, no Polymarket credentials.**
- **VPS location is irrelevant.** Order latency only matters for real trading.
  Pick any cheap, reliable provider — what matters is 24/7 uptime.

A small box is plenty: **2 vCPU / 4 GB RAM**, Ubuntu 22.04+ or Debian 12+.

---

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git tmux
```

## 2. Code + virtualenv

```bash
git clone <repo-url> polybot      # or scp/rsync the project directory
cd polybot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 3. X (Twitter) accounts — read this before copying `accounts.json`

The arena needs the Elon-tweet feed, which comes from the X GraphQL accounts in
`accounts.json`. **Moving those cookies to a datacenter IP is the riskiest part
of the migration** — X flags datacenter IPs and sudden geo jumps and can lock
the accounts.

- Use **residential proxies, ideally one per account.**
- Migrate **one account at a time**; watch for challenges/locks for 2–3 days
  before moving the rest.
- Keep a re-auth path ready (a challenged account needs re-login).

No Polymarket credentials are needed — paper mode places no orders.

## 4. Generate the v5 pool

```bash
python -m research.generate_v5_pool
# -> 200 configs (100 matched pairs) in data/experiments/v5_pool/
#    <base>_v4 = control (dynamic phi off) · <base>_v5 = treatment (on)
```

## 5. Smoke test (one tick, then exits)

```bash
python -m research.live_competitor_test \
  --configs-glob "data/experiments/v5_pool/*.json" --once
```

Confirm it loads 200 competitors and completes a tick with no errors.

## 6. Launch the run

```bash
tmux new -s arena
. .venv/bin/activate
python -m research.live_competitor_test \
  --configs-glob "data/experiments/v5_pool/*.json" \
  --tick-s 1 --duration-h 840          # 840h = 5 weeks; adjust as needed
# detach: Ctrl-b then d
```

## 7. Health watchdog (recommended, runs alongside)

`research/arena_watchdog.py` checks the arena is alive and ticking, restarts it
on a crash/stall, and watches disk. It is **already cross-platform** — the
Windows toast import is guarded, so on Linux it logs alerts instead. Run it in
a second tmux window. For real push alerts, wire `toast()` to email/Telegram.

## 8. Survive reboots — systemd (optional, more robust than tmux)

`/etc/systemd/system/polybot-arena.service`:

```ini
[Unit]
Description=polybot v5 paper arena
After=network-online.target

[Service]
WorkingDirectory=/home/USER/polybot
ExecStart=/home/USER/polybot/.venv/bin/python -m research.live_competitor_test \
  --configs-glob "data/experiments/v5_pool/*.json" --tick-s 1 --duration-h 840
Restart=on-failure
RestartSec=10
User=USER

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polybot-arena
journalctl -u polybot-arena -f      # live logs
```

## 9. Monitoring

```bash
# leaderboard / standings
tail -f data/reports/live_competitor_tests/*/leaderboard_latest.csv
# refresh the plots (orphan-corrected)
python -m research.refresh_plots
```

---

## Windows → Linux notes

| Windows item | On Linux |
|---|---|
| `winotify` toast alerts | Auto-disabled — watchdog logs instead. Wire `toast()` to email/Telegram for push alerts. |
| `scripts/run_watch.ps1` | Not used — replaced by tmux / the systemd unit above. |
| `powercfg` sleep settings | Not applicable — a VPS does not sleep. |

## What this deployment does NOT include

- **Real-money trading.** Paper only. Real orders are Gate 4 and a separate path.
- **The event calendar.** Built but shelved — calibration showed scheduled
  catalysts do not move Elon's tweet rate. It stays flag-gated off.
