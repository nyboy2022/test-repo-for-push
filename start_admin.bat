@echo off
setlocal
cd /d "%~dp0"
title Discord Forwarder Admin
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist ".venv-win\Scripts\python.exe" (
  echo [Setup] Creating Python virtual environment...
  where py >nul 2>nul && (py -3.11 -m venv .venv-win) || (python -m venv .venv-win)
  if errorlevel 1 goto :error
)

".venv-win\Scripts\python.exe" -c "import discord, flask, requests, certifi" >nul 2>nul
if errorlevel 1 (
  echo [Setup] Installing locked dependencies...
  ".venv-win\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check
  if errorlevel 1 goto :error
)

echo [Start] Local admin console: http://127.0.0.1:5151
".venv-win\Scripts\python.exe" config_web.py
goto :end

:error
echo [Error] Startup failed. Review the output above.
pause
:end
endlocal
