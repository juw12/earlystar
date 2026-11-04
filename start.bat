@echo off
REM EarlyStar: starts the local server with dev sign-in (collects every 30 min) and opens the app.
REM Optional: set GITHUB_TOKEN=ghp_xxx for higher GitHub rate limits, EARLYSTAR_ADMINS=yourlogin for admin tools.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required: https://www.python.org/downloads/
  pause
  exit /b 1
)
python server.py --dev-login --open %*
pause
