@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ============================================
echo  Network Lab - Protocol Test Suite
echo ============================================
echo.
set PYTHON=
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%USERPROFILE%\AppData\Local\Python\pythoncore-3.14-64\python.exe"
) do (
  if exist %%P set PYTHON=%%P
)
if "%PYTHON%"=="" (
  python --version > nul 2>&1
  if %errorlevel%==0 set PYTHON=python
)
echo Installing pytest...
%PYTHON% -m pip install pytest pytest-asyncio -q --no-warn-script-location
echo.
echo Running tests...
%PYTHON% -m pytest tests/ -v
pause
