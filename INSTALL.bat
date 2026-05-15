@echo off
REM ============================================================================
REM MuseProxy — first-run installer
REM
REM Run once per laptop. Installs uv if missing, syncs all Python dependencies,
REM renders all audio assets, and runs a self-check.
REM ============================================================================
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo  MuseProxy installer
echo ============================================================
echo.

REM --- 1. Verify Python is on PATH -------------------------------------------
where py >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python is not installed or not on PATH.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/
  echo and re-run this installer.
  pause
  exit /b 1
)

REM --- 2. Install uv if absent -----------------------------------------------
where uv >nul 2>&1
if errorlevel 1 (
  echo Installing uv...
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  if errorlevel 1 (
    echo ERROR: uv install failed.
    pause
    exit /b 1
  )
  echo.
  echo uv installed. You may need to close and re-open this Command Prompt
  echo before running this installer again.
  echo.
  pause
  exit /b 0
)

REM --- 3. Sync dependencies --------------------------------------------------
echo.
echo Running uv sync...
uv sync
if errorlevel 1 (
  echo ERROR: uv sync failed.
  pause
  exit /b 1
)

REM --- 4. Render audio assets ------------------------------------------------
echo.
echo Generating audio assets (this requires internet for edge-tts)...
uv run python scripts\generate_audio.py
if errorlevel 1 (
  echo ERROR: audio asset generation failed.
  pause
  exit /b 1
)

REM --- 5. Self-check ---------------------------------------------------------
echo.
echo Running self-check...
uv run python -m muse_proxy.app --self-check
if errorlevel 1 (
  echo.
  echo Self-check FAILED. See above for details.
  echo Fix the failing items, then re-run INSTALL.bat.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  INSTALL COMPLETE.
echo  Double-click launch.bat to start a session.
echo ============================================================
echo.
pause
