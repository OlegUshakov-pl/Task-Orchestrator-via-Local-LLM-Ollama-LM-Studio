@echo off
setlocal
cd /d "%~dp0"

set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

where python >nul 2>nul
if %errorlevel% neq 0 (
    if not exist "venv\Scripts\python.exe" (
        echo Python not found. Please install Python 3.14+ and add it to PATH.
        echo Or run install.bat once to create the virtual environment.
        pause
        exit /b 1
    )
)
"%PY%" -m pip show requests >nul 2>nul
if %errorlevel% neq 0 (
    echo Installing dependencies...
    "%PY%" -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo Failed to install dependencies. Check your internet connection and pip setup.
        pause
        exit /b 1
    )
)
"%PY%" script.py %*
pause
