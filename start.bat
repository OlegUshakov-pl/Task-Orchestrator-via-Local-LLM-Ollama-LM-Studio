@echo off
setlocal
cd /d "%~dp0"

chcp 65001 >nul 2>nul

if not exist "script.py" (
    echo ERROR: script.py not found in "%~dp0".
    pause
    exit /b 1
)

set "PY=venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Virtual environment not found. Creating it now...
    where python >nul 2>nul
    if %errorlevel% neq 0 (
        echo ERROR: Python not found. Please install Python 3.14+ and add it to PATH.
        pause
        exit /b 1
    )
    python -m venv venv
    if %errorlevel% neq 0 (
        echo ERROR: Failed to create the virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created.
)

"%PY%" script.py %*
set "CODE=%errorlevel%"
echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
