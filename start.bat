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
"%PY%" script.py %*
pause
