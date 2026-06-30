# Auto-startup script for the polybot arena cluster.
# Launches v4 + v5 + v6 + v7 LIVE arenas, snapshot watcher, and arena_watchdog.
# Idempotent: if any arena is already running, it exits without launching duplicates.
# Triggered at user login by a .bat shim in the Windows Startup folder.

$ErrorActionPreference = "Continue"
$projectRoot = "C:\Users\ojoca\Documents\polybot"
Set-Location $projectRoot

$logDir = Join-Path $projectRoot "logs"
$startupLog = Join-Path $logDir "auto_startup.log"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Log($msg) {
  $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  "$ts  $msg" | Out-File -FilePath $startupLog -Append -Encoding utf8
}

Log "===== polybot auto-startup invoked ====="

# Idempotency check: if any arena is already running, exit.
$existing = Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -match 'live_competitor_test' -and
  $_.CommandLine -notmatch 'Get-CimInstance'
}
if ($existing) {
  $count = ($existing | Measure-Object).Count
  Log "found $count live_competitor_test process(es) already running - skipping launch (idempotent)"
  foreach ($p in $existing) { Log ("  alive: PID {0}" -f $p.ProcessId) }
  exit 0
}

# Wait briefly for network / disk to be fully ready post-boot.
Start-Sleep -Seconds 15
Log "post-boot wait complete; launching arenas"

# Shared env for all 4 arenas. MIN_INTERVAL_S=30 spreads GQL load across the
# 5 X cookies so 4 arenas can share the budget without hitting 429s.
$env:POLYBOT_FAST_GQL = "1"
$env:POLYBOT_ENABLE_SSE_POOL = "0"
$env:POLYBOT_GQL_MIN_INTERVAL_S = "30"
$env:POLYBOT_GQL_JITTER_S = "5"
$env:POLYBOT_XTRACKER_TTL_S = "20"
$env:PYTHONIOENCODING = "utf-8"

function Launch-Arena {
  param([string]$Pool, [string]$LogTag)
  $outLog = Join-Path $logDir "arena_${LogTag}_stdout.log"
  $errLog = Join-Path $logDir "arena_${LogTag}_stderr.log"
  $p = Start-Process -FilePath "$projectRoot\.venv\Scripts\python.exe" `
    -ArgumentList @(
      "-u","-m","research.live_competitor_test",
      "--duration-h","168",
      "--tick-s","1",
      "--report-s","60",
      "--input-archive-s","5",
      "--health-poll-s","30",
      "--max-competitors","0",
      "--configs-glob","data/experiments/$Pool/*.json",
      "--out-dir","data/reports/live_competitor_tests"
    ) `
    -WorkingDirectory $projectRoot -WindowStyle Hidden `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
  Log ("launched $LogTag (pool=$Pool) PID {0}" -f $p.Id)
  return $p
}

# Launch the 4 arenas in parallel
Launch-Arena -Pool "v5_pool"  -LogTag "v5_live"  | Out-Null
Launch-Arena -Pool "v6_pool"  -LogTag "v6_live"  | Out-Null
Launch-Arena -Pool "v7_pool"  -LogTag "v7_live"  | Out-Null
Launch-Arena -Pool "96h_pool" -LogTag "v4_live"  | Out-Null

# Watchdog (monitors v4 specifically). Has 5-min startup grace so it won't
# kill the fresh v4 it just found alive.
Start-Sleep -Seconds 5
$wdOut = Join-Path $logDir "watchdog.log"
$wdErr = Join-Path $logDir "watchdog.err.log"
$pw = Start-Process -FilePath "$projectRoot\.venv\Scripts\python.exe" `
  -ArgumentList @("-u","-m","research.arena_watchdog","--duration-h","168") `
  -WorkingDirectory $projectRoot -WindowStyle Hidden `
  -RedirectStandardOutput $wdOut -RedirectStandardError $wdErr -PassThru
Log ("launched arena_watchdog PID {0}" -f $pw.Id)

Log "===== auto-startup complete ====="
