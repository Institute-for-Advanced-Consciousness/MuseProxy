@echo off
REM MuseProxy direct-BLE bridge (Muse-2 / Muse-Athena -> LSL).
REM
REM   launch_bridge.bat                       -> scan for headsets, then explain next step
REM   launch_bridge.bat stream --model muse-2 --address 00:55:DA:XX:XX:XX
REM   launch_bridge.bat stream --model muse-athena --address 00:55:DA:XX:XX:XX
REM
REM Keep this window open for the whole pass. Ctrl+C stops it cleanly.
cd /d "%~dp0"
if "%~1"=="" (
  uv run python -m muse_proxy.bridge scan
  echo.
  echo Next: launch_bridge.bat stream --model muse-2 --address ^<address from the list^>
  echo   or: launch_bridge.bat stream --model muse-athena --address ^<address^>
  pause
  exit /b
)
uv run python -m muse_proxy.bridge %*
