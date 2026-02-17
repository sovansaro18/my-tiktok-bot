@echo off
setlocal
cd /d %~dp0

echo ===============================
echo  Telegram Downloader Bot Setup
echo ===============================

if not exist .env (
  if exist .env.example (
    copy .env.example .env >nul
    echo [OK] Created .env from .env.example
    echo [ACTION] Please edit .env and fill BOT_TOKEN and ADMIN_ID.
  ) else (
    echo [WARN] .env.example not found.
  )
)

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ and enable "Add Python to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual env.
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

echo [OK] Setup complete.
echo Next: double click run_windows.bat
pause
