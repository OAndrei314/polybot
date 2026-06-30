# Wrapper that runs the wallet discovery scan once.
# Scheduled to run every 4 hours by Windows Task Scheduler.
# Each invocation walks the recent-trades feed and ACCUMULATES wallets
# into wallets.jsonl.gz (existing wallets keep their max stats).
# After a week of 4h runs (~42 invocations) we should have a much larger set.

$ErrorActionPreference = "Continue"
$projectRoot = "C:\Users\ojoca\Documents\polybot"
Set-Location $projectRoot

$logDir = Join-Path $projectRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir "wallet_discovery_cron.log"

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== $ts run start ===" | Out-File -FilePath $log -Append -Encoding utf8

# Use --merge to accumulate rather than overwrite
& "$projectRoot\.venv\Scripts\python.exe" -m research.polymarket_alpha.discover_wallets `
  --pages 50 --min-size 100 --merge 2>&1 | Out-File -FilePath $log -Append -Encoding utf8

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') run end ===" | Out-File -FilePath $log -Append -Encoding utf8
"" | Out-File -FilePath $log -Append -Encoding utf8
