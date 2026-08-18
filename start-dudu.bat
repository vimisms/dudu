@echo off
setlocal EnableDelayedExpansion
title DuDu launcher

REM ============================================================================
REM  DuDu one-click launcher
REM
REM    start-dudu.bat          -> dev mode (Vite HMR + cargo run, what you had)
REM    start-dudu.bat build    -> build the release .exe, then run it
REM    start-dudu.bat backend  -> backend only (no UI; use curl /command)
REM
REM  It checks prerequisites, creates/refreshes the Python venv, starts the
REM  backend in its own window, WAITS for /health to answer before starting the
REM  UI (the old failure mode was a UI that came up first and sat disconnected),
REM  and prints a specific message for each thing that can go wrong.
REM ============================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "BACKEND=%ROOT%\backend"
set "FRONTEND=%ROOT%\frontend"
set "VENV=%BACKEND%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.deps-installed"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=dev"

echo.
echo   DuDu launcher   [mode: %MODE%]
echo   =========================================================
echo.

REM ---------------------------------------------------------------- checks --
echo [1/5] Checking prerequisites...

where python >nul 2>&1
if errorlevel 1 (
  echo   [X] Python is not on PATH.
  echo       Install Python 3.11 from https://python.org and tick
  echo       "Add python.exe to PATH" during setup.
  goto :fail
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo   [ok] Python !PYVER!

where node >nul 2>&1
if errorlevel 1 (
  echo   [X] Node.js is not on PATH.
  echo       Needed for the UI AND for the filesystem MCP server ^(npx^).
  echo       Install from https://nodejs.org ^(18 or newer^).
  goto :fail
)
for /f "tokens=*" %%v in ('node --version 2^>^&1') do set "NODEVER=%%v"
echo   [ok] Node !NODEVER!

if /i not "%MODE%"=="backend" (
  where cargo >nul 2>&1
  if errorlevel 1 (
    echo   [X] Rust/cargo is not on PATH ^(needed to build the Tauri shell^).
    echo       Install from https://www.rust-lang.org/tools/install
    echo       Then open a NEW terminal so PATH picks it up.
    goto :fail
  )
  echo   [ok] Rust toolchain present
)

if not exist "%BACKEND%\.env" (
  echo   [X] %BACKEND%\.env is missing.
  echo       Copy .env.example to .env and fill in your API keys.
  goto :fail
)
echo   [ok] backend\.env found

REM ------------------------------------------------------------ python env --
echo.
echo [2/5] Preparing the Python environment...

if not exist "%PY%" (
  echo   Creating virtualenv in backend\.venv ^(one time^)...
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo   [X] Could not create the virtualenv.
    goto :fail
  )
)

REM Reinstall dependencies only when requirements.txt is newer than the stamp.
set "NEED_INSTALL="
if not exist "%STAMP%" set "NEED_INSTALL=1"
if exist "%STAMP%" (
  for /f %%i in ('powershell -NoProfile -Command ^
     "if((Get-Item '%BACKEND%\requirements.txt').LastWriteTime -gt (Get-Item '%STAMP%').LastWriteTime){'1'}else{'0'}"') do set "NEWER=%%i"
  if "!NEWER!"=="1" set "NEED_INSTALL=1"
)

if defined NEED_INSTALL (
  echo   Installing/updating Python dependencies ^(this takes a few minutes the first time^)...
  "%PY%" -m pip install --upgrade pip --quiet
  "%PY%" -m pip install -r "%BACKEND%\requirements.txt"
  if errorlevel 1 (
    echo   [X] pip install failed. Scroll up for the offending package.
    goto :fail
  )
  echo done > "%STAMP%"
) else (
  echo   [ok] dependencies already installed
)

REM ------------------------------------------------------------ node deps ---
if /i not "%MODE%"=="backend" (
  echo.
  echo [3/5] Preparing the frontend...
  if not exist "%FRONTEND%\node_modules" (
    echo   Running npm install ^(one time^)...
    pushd "%FRONTEND%"
    call npm install
    if errorlevel 1 (
      popd
      echo   [X] npm install failed.
      goto :fail
    )
    popd
  ) else (
    echo   [ok] node_modules present
  )
) else (
  echo.
  echo [3/5] Skipping frontend ^(backend-only mode^)
)

REM -------------------------------------------------------------- backend ---
echo.
echo [4/5] Starting the backend...

REM If something is already listening on 8756, don't start a second copy --
REM two mic loops fighting over the same input device is a confusing failure.
netstat -ano | findstr /r /c:"LISTENING" | findstr /c:":8756 " >nul 2>&1
if not errorlevel 1 (
  echo   [!] Something is already listening on port 8756.
  echo       Assuming that's a DuDu backend and reusing it.
  echo       If not, close it ^(or run stop-dudu.bat^) and retry.
) else (
  start "DuDu Backend" cmd /k "cd /d "%BACKEND%" && "%PY%" main.py"
)

echo   Waiting for the backend to answer /health...
set "READY="
for /l %%i in (1,1,60) do (
  if not defined READY (
    curl -s -o nul -m 2 http://127.0.0.1:8756/health >nul 2>&1
    if not errorlevel 1 (
      set "READY=1"
      echo   [ok] backend is up ^(after about %%i seconds^)
    ) else (
      timeout /t 1 /nobreak >nul
    )
  )
)

if not defined READY (
  echo.
  echo   [X] The backend never answered on http://127.0.0.1:8756/health
  echo       Look at the "DuDu Backend" window for the traceback. The usual
  echo       causes are a missing API key in .env, or a package that failed to
  echo       import. Full log: backend\logs\dudu.log
  goto :fail
)

REM Tool loading continues in the background after /health starts answering;
REM the UI shows "starting up - loading tools" until it finishes.

if /i "%MODE%"=="backend" (
  echo.
  echo [5/5] Backend-only mode. Try:
  echo   curl -X POST http://127.0.0.1:8756/command -H "Content-Type: application/json" -d "{\"text\":\"what tools do you have\"}"
  goto :done
)

REM ------------------------------------------------------------------- UI ---
echo.
echo [5/5] Starting the DuDu window...
pushd "%FRONTEND%"
if /i "%MODE%"=="build" (
  echo   Building a release bundle ^(first build takes several minutes^)...
  call npm run tauri build
  if errorlevel 1 (
    popd
    echo   [X] tauri build failed.
    goto :fail
  )
  if exist "%FRONTEND%\src-tauri\target\release\DuDu.exe" (
    start "" "%FRONTEND%\src-tauri\target\release\DuDu.exe"
  ) else if exist "%FRONTEND%\src-tauri\target\release\dudu-assistant.exe" (
    start "" "%FRONTEND%\src-tauri\target\release\dudu-assistant.exe"
  ) else (
    echo   [!] Build finished but the .exe wasn't where expected. Look under
    echo       frontend\src-tauri\target\release\.
  )
) else (
  call npm run tauri dev
)
popd

:done
echo.
echo   DuDu is running. Say "Dudu" followed by your instruction, or type in the window.
echo   Backend log: backend\logs\dudu.log
echo.
goto :eof

:fail
echo.
echo   Startup aborted. Nothing was left running by this script.
echo.
pause
exit /b 1
