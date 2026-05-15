@echo off
REM ============================================================================
REM MuseProxy — session launcher
REM Double-click to start the experiment GUI.
REM ============================================================================
cd /d "%~dp0"
uv run python -m muse_proxy.app
if errorlevel 1 (
  echo.
  echo MuseProxy exited with an error. Scroll up for details.
  echo If unexpected, run: uv run python -m muse_proxy.app --self-check
  pause
)
