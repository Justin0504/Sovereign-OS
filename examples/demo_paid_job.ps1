# Demo: one paid job (Stripe test mode)
# 1. Set STRIPE_API_KEY (sk_test_...) and run Web UI with SOVEREIGN_JOB_DB + SOVEREIGN_LEDGER_PATH
# 2. Run this script to POST a job (goal + amount_cents)
# 3. Open http://localhost:8000 -> Job queue -> Approve the job
# 4. After mission completes, Stripe charges and Ledger shows job_income

$base = "http://localhost:8000"
$body = @{
    goal         = "Summarize the market in one paragraph."
    amount_cents = 100
    currency     = "USD"
} | ConvertTo-Json

Write-Host "POST $base/api/jobs" -ForegroundColor Cyan
Write-Host "Body: $body" -ForegroundColor Gray
try {
    $r = Invoke-RestMethod -Uri "$base/api/jobs" -Method POST -ContentType "application/json" -Body $body
    $j = if ($r.job) { $r.job } else { $r }
    Write-Host "Job created: id=$($j.job_id) goal=$($j.goal) amount=$($j.amount_cents) cents status=$($j.status)" -ForegroundColor Green
    Write-Host "Next: Open $base -> Job queue -> Approve job $($j.job_id). After run, check Balance and ledger." -ForegroundColor Yellow
} catch {
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host "Is the Web UI running? Start with: python -m sovereign_os.web.app" -ForegroundColor Yellow
}
