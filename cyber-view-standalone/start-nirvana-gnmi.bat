@echo off
rem Opens the gNMI setup screen (double-click). Close it with Ctrl+C in this window.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install it from https://www.python.org/ & pause & exit /b 1)
python -c "import fastapi, uvicorn, yaml" 2>nul || python -m pip install -r requirements.txt
python setup_gui.py
pause
