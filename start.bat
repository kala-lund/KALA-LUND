@echo off
REM Start the Quant Crypto paper-trading backend + dashboard.
REM Config is validated first; the window stays open if startup fails.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH. Install Python 3.10+ and retry.
  pause
  exit /b 1
)
python scripts\run_trader.py
if errorlevel 1 (
  echo.
  echo Startup failed - see the errors above. Fix .env, then run start.bat again.
  pause
  exit /b 1
)
