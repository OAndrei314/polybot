# VPS Deployment

This directory holds files for running polybot on a Linux VPS as a long-lived service.

## What's here

| File | Purpose |
|---|---|
| `polybot-watch.service` | systemd unit that supervises `signals/watch.py`. Auto-restarts on crash, graceful SIGTERM, security hardening. |

## One-time setup on the VPS

```bash
# 1. Create the user (no shell, just enough to run the process)
sudo useradd -r -m -d /home/polybot -s /bin/bash polybot

# 2. Clone repo
sudo -u polybot git clone <your-repo> /home/polybot/polybot
cd /home/polybot/polybot

# 3. Python venv + deps
sudo -u polybot python3 -m venv .venv
sudo -u polybot .venv/bin/pip install -r requirements.txt
# (or whatever your install steps are: requests, playwright, python-dotenv, curl_cffi)

# 4. Drop accounts.json and .env in place (NOT in git)
sudo -u polybot vim accounts.json   # paste credentials
sudo -u polybot vim .env            # paste POLYBOT_DISCORD_WEBHOOK=... etc

# 5. Make data + logs dirs writable
sudo -u polybot mkdir -p /home/polybot/polybot/data /home/polybot/polybot/logs

# 6. Install the unit
sudo cp deploy/polybot-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polybot-watch
sudo systemctl start polybot-watch
```

## Live operation

```bash
# Status
sudo systemctl status polybot-watch

# Tail logs in real time
journalctl -u polybot-watch -f

# Last 100 log lines
journalctl -u polybot-watch -n 100

# Stop / start / restart
sudo systemctl stop polybot-watch
sudo systemctl start polybot-watch
sudo systemctl restart polybot-watch

# Read the live health snapshot
cat /home/polybot/polybot/data/health.json | jq .

# Read the seen-tweet checkpoint
cat /home/polybot/polybot/data/watch_state.json | jq .

# Check if process is alive externally (script-friendly)
sudo systemctl is-active polybot-watch
```

## Environment variables (set in `.env`)

```bash
# Discord/Telegram alerting (any one of these enables webhook delivery)
POLYBOT_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
POLYBOT_TELEGRAM_BOT_TOKEN=123456:ABC...
POLYBOT_TELEGRAM_CHAT_ID=-100123456

# Reliability tuning (all optional, defaults are sane)
POLYBOT_ALERT_NO_SIGNAL_S=600        # alert if no tweet detected for 10min
POLYBOT_HEALTH_FILE=data/health.json
POLYBOT_DAILY_SUMMARY=1              # daily summary at UTC midnight
```

## What auto-recovers

| Failure mode | Recovery |
|---|---|
| GQL hash rotated | Background refresher swaps in new hash within 30s of 404 |
| Account cookie expires | After 3x 401, account marked dead, alert sent. Other accounts keep going. |
| GQL/REST poll thread dies | Watchdog restarts within 5s |
| Whole process crashes | systemd restarts within 10s |
| All accounts dead | XTracker keeps reporting lifetime count + alert sent. Manual intervention required. |
| Tweet pipeline silently broken | XTracker divergence detector alerts after 3min |
| No detection for 10min | Critical alert via webhook |

## What requires manual intervention

- Account cookies expiring beyond renewal (you must refresh from a real browser)
- Twitter banning multiple accounts in a short window
- Polymarket API changes (XTracker URL, gamma endpoint, etc.)
- Disk full, network outage, VPS reboot (systemd handles VPS reboot automatically)
