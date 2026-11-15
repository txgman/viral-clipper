@echo off
rem One-click start for Windows. Double-click this file.
cd /d "%~dp0"

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg is required. Install it by running this in a terminal:
  echo     winget install Gyan.FFmpeg
  echo Then close this window and double-click run.bat again.
  pause
  exit /b 1
)
where deno >nul 2>nul
if errorlevel 1 (
  where node >nul 2>nul
  if errorlevel 1 echo Note: YouTube downloads need Deno or Node.js. Install with: winget install DenoLand.Deno
)

if not exist .venv (
  echo Creating virtual environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo Python 3.10 or newer is required: https://www.python.org/downloads/
    pause
    exit /b 1
  )
)
call .venv\Scripts\activate.bat
if not exist .venv\.installed (
  echo Installing dependencies - first run takes a few minutes...
  python -m pip install --upgrade pip -q
  python -m pip install -r requirements.txt -q || (pause & exit /b 1)
  echo done> .venv\.installed
)
python -m app %*
pause
