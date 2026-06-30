# Windows supervisor: restart-on-exit wrapper for signals/watch.py
#
# Usage:
#   PowerShell:  .\scripts\run_watch.ps1 -Username elonmusk
#   Background:  Start-Process powershell -ArgumentList "-File","scripts\run_watch.ps1"
#
# Stop with Ctrl+C — graceful shutdown propagates to the watch process,
# which removes its lockfile and writes a final checkpoint.

[CmdletBinding()]
param(
    [string]$Username = "elonmusk",
    [string]$UserId = "",
    [int]$MinIntervalS = 7,
    [string]$LogDir = "logs",
    [int]$CrashBackoffS = 5,
    [int]$MaxBackoffS = 120,
    [switch]$NoSse,
    [switch]$Rest
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $RepoRoot

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "ERROR: $Python not found. Run: python -m venv .venv && .venv\Scripts\pip install requests playwright python-dotenv" -ForegroundColor Red
    exit 1
}

$argList = @(
    "-m", "signals.watch",
    "--username", $Username,
    "--min-interval-s", "$MinIntervalS"
)
if ($UserId) { $argList += @("--user-id", $UserId) }
if ($NoSse)  { $argList += "--no-sse" }
if ($Rest)   { $argList += "--rest" }

$Backoff = $CrashBackoffS
$Iter = 0
while ($true) {
    $Iter++
    $Stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH-mm-ssZ")
    $StdoutLog = Join-Path $LogDir "watch_${Stamp}.out.log"
    $StderrLog = Join-Path $LogDir "watch_${Stamp}.err.log"
    Write-Host "[$Stamp] supervisor iter=$Iter starting: $Python $($argList -join ' ')" -ForegroundColor Cyan

    $proc = Start-Process -FilePath $Python -ArgumentList $argList `
        -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog `
        -NoNewWindow -PassThru -Wait
    $ExitCode = $proc.ExitCode

    $EndStamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH-mm-ssZ")
    if ($ExitCode -eq 0) {
        Write-Host "[$EndStamp] watch exited cleanly (code 0); stopping supervisor." -ForegroundColor Green
        break
    }

    Write-Host "[$EndStamp] watch crashed (code $ExitCode); restarting in ${Backoff}s. stderr: $StderrLog" -ForegroundColor Yellow
    Start-Sleep -Seconds $Backoff
    # exponential backoff capped at MaxBackoffS
    $Backoff = [Math]::Min($MaxBackoffS, $Backoff * 2)
    # but reset to base if process lived >5 minutes
    # (rough: if total elapsed since start was long, reset)
}
