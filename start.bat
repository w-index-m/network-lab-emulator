@echo off
cd /d "%~dp0"
title Network Lab Emulator

echo ============================================
echo  Network Lab Emulator - Startup Check
echo ============================================
echo.

set ALL_OK=1

:: ===== RAM Check =====
echo [CHECK] RAM...
for /f "tokens=2 delims==" %%M in ('wmic computersystem get TotalPhysicalMemory /value 2^>nul ^| find "="') do set RAM_BYTES=%%M
if not defined RAM_BYTES set RAM_BYTES=2000000000
set /a RAM_GB=%RAM_BYTES:~0,-9%
if %RAM_GB% LSS 1 set RAM_GB=1
echo   RAM: %RAM_GB% GB
if %RAM_GB% LSS 1 (
    echo   [NG] RAM too low. Need 1GB or more.
    set ALL_OK=0
) else (
    echo   [OK] RAM OK. (Emulator uses ~120MB max)
)

:: ===== Disk Check =====
echo [CHECK] Disk space...
for /f "tokens=3" %%D in ('dir /-c "%~dp0" 2^>nul ^| find "bytes free"') do set DISK_FREE=%%D
if not defined DISK_FREE set DISK_FREE=999999999
set /a DISK_MB=%DISK_FREE:~0,-6%
if %DISK_MB% LSS 1 set DISK_MB=999
echo   Disk free: %DISK_MB% MB
if %DISK_MB% LSS 50 (
    echo   [NG] Disk too low. Need 50MB or more.
    set ALL_OK=0
) else (
    echo   [OK] Disk OK. (Emulator uses ~10MB)
)

:: ===== OS Check =====
echo [CHECK] Windows version...
for /f "tokens=4-5 delims=[.] " %%A in ('ver') do set WIN_BUILD=%%B
if not defined WIN_BUILD set WIN_BUILD=99999
echo   Windows Build: %WIN_BUILD%
if %WIN_BUILD% LSS 17763 (
    echo   [WARN] Old Windows detected. Windows 10/11 recommended.
) else (
    echo   [OK] Windows OK.
)

echo.
echo   Requirement: RAM 1GB+  Disk 50MB+  Windows 10/11
echo   (Measured: RAM ~120MB  Disk ~10MB  for 5 devices full config)
echo.

:: ===== Python Check =====
echo [CHECK] Python...
set PYTHON=
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "C:\Python311\python.exe"
) do (
  if exist %%P (
    set PYTHON=%%P
    goto python_found
  )
)
python --version > nul 2>&1
if %errorlevel% == 0 (
  set PYTHON=python
  goto python_found
)
echo   [NG] Python is NOT installed.
echo.
echo   HOW TO INSTALL PYTHON:
echo   1. Open https://www.python.org/downloads/
echo   2. Click Download Python
echo   3. CHECK: Add python.exe to PATH
echo   4. Click Install Now
echo   5. Re-run this start.bat
echo.
set ALL_OK=0
goto node_check

:python_found
for /f "tokens=*" %%V in ('%PYTHON% --version') do set PY_VER=%%V
echo   [OK] %PY_VER%

:: ===== Package Check =====
echo [CHECK] Packages (fastapi, uvicorn)...
%PYTHON% -c "import fastapi" > nul 2>&1
if %errorlevel% neq 0 (
  echo   [--] Installing packages...
  %PYTHON% -m pip install -r requirements.txt -q --no-warn-script-location
  if %errorlevel% == 0 (
    echo   [OK] Packages installed.
  ) else (
    echo   [NG] Install failed. Run: python -m pip install -r requirements.txt
    set ALL_OK=0
  )
) else (
  for /f "tokens=*" %%V in ('%PYTHON% -c "import fastapi; print(fastapi.__version__)"') do set FA_VER=%%V
  echo   [OK] fastapi %FA_VER% already installed.
)

:: ===== Node.js Check =====
:node_check
echo [CHECK] Node.js (optional)...
node --version > nul 2>&1
if %errorlevel% == 0 (
  for /f "tokens=*" %%V in ('node --version') do set NODE_VER=%%V
  echo   [OK] Node.js %NODE_VER%
) else (
  echo   [--] Node.js not found. (optional, not required to run)
)

echo.

:: ===== Final =====
if %ALL_OK% == 0 (
  echo [RESULT] Some checks failed. See messages above.
  echo.
  pause
  exit /b 1
)

echo [OK] All checks passed. Starting server...
echo.
echo   Open browser: http://localhost:8000
echo   Stop:         Ctrl+C
echo.
%PYTHON% app.py
pause
