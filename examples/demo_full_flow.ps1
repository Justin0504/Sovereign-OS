# Full demo: auto ingest from "web" -> CEO/CFO -> permissions (firewall) -> delivery -> Stripe
# 1. Starts a local HTTP server (port 8888) serving examples/ so ingest can "pull orders from the web"
# 2. Sets env for ingest URL, auto-approve, and persistence
# 3. You run the Web app in this terminal (or in a second one with the printed env)
# 4. Open http://localhost:8000 and watch: Job queue, Decision stream (CEO/CFO), Trust, Balance, webhook

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path "$root\sovereign_os")) { $root = (Get-Location).Path }
Set-Location $root

$examplesDir = Join-Path $root "examples"
$port = 8888
$ingestUrl = "http://localhost:${port}/ingest_demo_orders.json"

# Start HTTP server in background (serves examples/ so ingest_demo_orders.json is available)
Write-Host "Starting local order server at http://localhost:$port (serving examples/)..." -ForegroundColor Cyan
$serverJob = Start-Job -ScriptBlock {
    Set-Location $using:examplesDir
    python -m http.server $using:port 2>&1
}
Start-Sleep -Seconds 2

# Env for full auto flow: ingest + auto-approve + persistence
$env:SOVEREIGN_INGEST_URL = $ingestUrl
$env:SOVEREIGN_INGEST_INTERVAL_SEC = "15"
$env:SOVEREIGN_AUTO_APPROVE_JOBS = "true"
$env:SOVEREIGN_COMPLIANCE_AUTO_PROCEED = "true"
$env:SOVEREIGN_JOB_DB = (Join-Path $root "data\jobs.db")
$env:SOVEREIGN_LEDGER_PATH = (Join-Path $root "data\ledger.jsonl")
New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null

Write-Host ""
Write-Host "Demo flow (real logic):" -ForegroundColor Yellow
Write-Host "  1. Ingest: poller fetches $ingestUrl every 15s -> jobs enqueued"
Write-Host "  2. CEO: Strategist splits each job into tasks (see Decision stream)"
Write-Host "  3. CFO: Treasury approves budget (see Decision stream)"
Write-Host "  4. Firewall: SovereignAuth checks TrustScore vs capability (see Trust in UI)"
Write-Host "  5. Delivery: on completion, POST to SOVEREIGN_WEBHOOK_URL if set"
Write-Host "  6. Stripe: charge amount_cents; Ledger gets job_income"
Write-Host ""
Write-Host "Starting Web UI. Open http://localhost:8000 and watch Job queue + Decision stream." -ForegroundColor Green
Write-Host "To stop: Ctrl+C (server job will end with this process)." -ForegroundColor Gray
Write-Host ""

try {
    python -m sovereign_os.web.app
} finally {
    Stop-Job $serverJob -ErrorAction SilentlyContinue
    Remove-Job $serverJob -Force -ErrorAction SilentlyContinue
}
