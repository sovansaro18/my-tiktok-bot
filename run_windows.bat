@echo off
setlocal
cd /d %~dp0

if not exist .venv (
  echo [ERROR] venv not found. Run setup_windows.bat first.
  pause
  exit /b 1
)

call .venv\Scripts\activate
set PYTHONUNBUFFERED=1
python main.py
pause
