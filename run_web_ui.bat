@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Sovereign-OS Web UI...
echo Open in browser: http://127.0.0.1:8000  (or http://localhost:8000)
echo.
python -m sovereign_os.web.app
pause
