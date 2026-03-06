@echo off
REM Paid demo: load .env and start Web UI. Double-click or run from project root.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_paid_demo.ps1"
pause
