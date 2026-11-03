@echo off
REM RepoRadar — starts the local server (collects every 30 min) and opens the app.
REM Optional: set GITHUB_TOKEN=ghp_xxx before running for higher GitHub rate limits.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required: https://www.python.org/downloads/
  pause
  exit /b 1
)
python server.py --open %*
pause
