@echo off
setlocal EnableDelayedExpansion
title DuDu shutdown

REM Stops whatever is holding port 8756 (the backend) plus the Tauri dev shell.
REM Useful when a previous run left a stray process holding the microphone or
REM the vector index, which shows up as "port already in use" on next launch.

echo.
echo   Stopping DuDu...
echo.

set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /c:":8756 "') do (
  if not "%%p"=="0" (
    echo   Killing backend process %%p
    taskkill /PID %%p /T /F >nul 2>&1
    set "FOUND=1"
  )
)
if not defined FOUND echo   No backend was listening on port 8756.

taskkill /IM dudu-assistant.exe /F >nul 2>&1 && echo   Closed the DuDu dev window.
taskkill /IM DuDu.exe /F >nul 2>&1 && echo   Closed the DuDu app.

echo.
echo   Done.
timeout /t 3 /nobreak >nul
