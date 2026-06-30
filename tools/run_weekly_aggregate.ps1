# Weekly aggregate refresh - re-pulls wallet histories (force) and re-runs
# the full smart-money pipeline so we incorporate ~48 hourly-discovery
# additions per day from the wallet_discovery cron.
#
# Scheduled: every Sunday 03:00 local. Single invocation runs:
#   1. fetch_markets   (force=true to refresh resolution outcomes)
#   2. fetch_wallet_histories  (top 300, force=true to refresh active ones)
#   3. aggregate
#   4. detect_patterns
#   5. copy_backtest

$ErrorActionPreference = "Continue"
$projectRoot = "C:\Users\ojoca\Documents\polybot"
Set-Location $projectRoot

$log = Join-Path $projectRoot "logs\weekly_aggregate.log"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"=========== $ts WEEKLY AGGREGATE START ===========" | Out-File -FilePath $log -Append -Encoding utf8

$py = "$projectRoot\.venv\Scripts\python.exe"

"--- step 1/5: refresh resolved markets ---" | Out-File -FilePath $log -Append -Encoding utf8
& $py -m research.polymarket_alpha.fetch_markets --max-markets 2000 --min-volume 10000 2>&1 |
  Out-File -FilePath $log -Append -Encoding utf8

"--- step 2/5: refresh top 300 wallet histories (force) ---" | Out-File -FilePath $log -Append -Encoding utf8
& $py -m research.polymarket_alpha.fetch_wallet_histories --top 300 --force 2>&1 |
  Out-File -FilePath $log -Append -Encoding utf8

"--- step 3/5: aggregate ---" | Out-File -FilePath $log -Append -Encoding utf8
& $py -m research.polymarket_alpha.aggregate 2>&1 |
  Out-File -FilePath $log -Append -Encoding utf8

"--- step 4/5: detect_patterns ---" | Out-File -FilePath $log -Append -Encoding utf8
& $py -m research.polymarket_alpha.detect_patterns 2>&1 |
  Out-File -FilePath $log -Append -Encoding utf8

"--- step 5/5: copy_backtest ---" | Out-File -FilePath $log -Append -Encoding utf8
& $py -m research.polymarket_alpha.copy_backtest 2>&1 |
  Out-File -FilePath $log -Append -Encoding utf8

"=========== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') WEEKLY AGGREGATE END ===========" |
  Out-File -FilePath $log -Append -Encoding utf8
"" | Out-File -FilePath $log -Append -Encoding utf8
