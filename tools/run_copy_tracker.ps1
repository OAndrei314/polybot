# Runs the live copy-trade tracker once.
# Scheduled every 30 min by Windows Task Scheduler.

$ErrorActionPreference = "Continue"
$projectRoot = "C:\Users\ojoca\Documents\polybot"
Set-Location $projectRoot

$log = Join-Path $projectRoot "logs\copy_tracker.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=== $ts ===" | Out-File -FilePath $log -Append -Encoding utf8

& "$projectRoot\.venv\Scripts\python.exe" -m research.polymarket_alpha.live_copy_tracker 2>&1 |
  Out-File -FilePath $log -Append -Encoding utf8
