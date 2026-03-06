@echo off
REM One-command demo: run a mission with ledger and audit trail.
REM Run from project root: examples\demo.bat  or  cd examples && demo.bat

set ROOT=%~dp0..
if not exist "%ROOT%\charter.example.yaml" set ROOT=.
set DATA=%ROOT%\data
if not exist "%DATA%" mkdir "%DATA%"

echo Running Sovereign-OS demo: mission with ledger + audit trail...
echo.
sovereign run --charter "%ROOT%\charter.example.yaml" --ledger "%DATA%\ledger.jsonl" --audit-trail "%DATA%\audit.jsonl" "Summarize the market in one paragraph."
echo.
echo Ledger: %DATA%\ledger.jsonl
echo Audit trail: %DATA%\audit.jsonl
echo To inspect: type %DATA%\audit.jsonl
pause
