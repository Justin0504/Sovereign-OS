@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Sovereign-OS Command Center...
echo.
echo Press R to run demo mission, F12 to exit.
echo.
python -m sovereign_os.ui.app
pause
