@echo off
REM Free port 8000: kill the process using it. Double-click to run.
echo Finding process using port 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo Killing PID %%a
    taskkill /F /PID %%a 2>nul
)
echo Done. Now run run_paid_demo.bat again.
pause
