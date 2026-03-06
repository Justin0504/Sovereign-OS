# Load .env and start Web UI for paid demo (Stripe test mode)
# Usage: .\run_paid_demo.ps1   or double-click run_paid_demo.bat (from project root)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (Test-Path ".env") {
    Get-Content ".env" -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
    Write-Host "[OK] Loaded .env" -ForegroundColor Green
} else {
    Write-Host "[!] No .env. Copy .env.example to .env and set STRIPE_API_KEY." -ForegroundColor Yellow
}

$env:SOVEREIGN_JOB_DB = if ($env:SOVEREIGN_JOB_DB) { $env:SOVEREIGN_JOB_DB } else { "$root\data\jobs.db" }
$env:SOVEREIGN_LEDGER_PATH = if ($env:SOVEREIGN_LEDGER_PATH) { $env:SOVEREIGN_LEDGER_PATH } else { "$root\data\ledger.jsonl" }
$env:SOVEREIGN_AUDIT_TRAIL_PATH = if ($env:SOVEREIGN_AUDIT_TRAIL_PATH) { $env:SOVEREIGN_AUDIT_TRAIL_PATH } else { "$root\data\audit.jsonl" }
if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null; Write-Host "[OK] Created data\" -ForegroundColor Green }

Write-Host ""
Write-Host "Paid demo (Stripe test mode)" -ForegroundColor White
Write-Host "  Job DB:    $env:SOVEREIGN_JOB_DB" -ForegroundColor Gray
Write-Host "  Ledger:    $env:SOVEREIGN_LEDGER_PATH" -ForegroundColor Gray
Write-Host "  Stripe:    $(if ($env:STRIPE_API_KEY) { 'set' } else { 'not set' })" -ForegroundColor Gray
Write-Host ""
Write-Host "  Open:      http://localhost:8000" -ForegroundColor Cyan
Write-Host "  Add job:   .\examples\demo_paid_job.ps1  then Approve in Job queue" -ForegroundColor Cyan
Write-Host ""
python -m sovereign_os.web.app
