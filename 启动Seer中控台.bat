@echo off
setlocal
cd /d "%~dp0"
rem Run the PowerShell launcher in the same folder (auto-elevates to admin)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-seer-console.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] Startup script failed. Press any key to exit.
    pause >nul
)
