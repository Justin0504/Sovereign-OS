@echo off
REM 释放 8000 端口：结束占用该端口的进程。双击运行即可。
echo Finding process using port 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo Killing PID %%a
    taskkill /F /PID %%a 2>nul
)
echo Done. Now run run_paid_demo.bat again.
pause
