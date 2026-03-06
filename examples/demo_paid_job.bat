@echo off
REM Submit one paid job (1 USD) to the running Web UI. Run from project root.
REM If you get "script disabled" error when using .ps1, use this .bat instead.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$r = Invoke-RestMethod -Uri 'http://localhost:8000/api/jobs' -Method POST -ContentType 'application/json' -Body '{\"goal\":\"Summarize the market in one paragraph.\",\"amount_cents\":100,\"currency\":\"USD\"}'; ^
   $j = if ($r.job) { $r.job } else { $r }; ^
   Write-Host ('Job created: id=' + $j.job_id + ' amount=' + $j.amount_cents + ' cents status=' + $j.status) -ForegroundColor Green; ^
   Write-Host ('Next: Open http://localhost:8000 -> Job queue -> Approve job ' + $j.job_id) -ForegroundColor Yellow"

if errorlevel 1 (
  echo.
  echo Is the Web UI running? Start with: run_paid_demo.bat
  pause
)
