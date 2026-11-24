@echo off
title Mental Math
cd /d "%~dp0"
echo.
echo  Mental Math - starting...
echo.
if not exist ".venv\Scripts\python.exe" (
    echo  First run: creating a private Python environment ^(about a minute^)...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo  Could not find Python. Install Python from python.org, tick "Add to PATH", then run this again.
        pause
        exit /b 1
    )
)
echo  Checking packages...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo  Package install failed. Check your internet connection, then run this again.
    pause
    exit /b 1
)
echo.
echo  Open http://127.0.0.1:8000 in Chrome or Edge. Press Ctrl+C here to stop.
echo.
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m uvicorn main:app --reload
pause
