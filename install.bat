@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found. Please install Python 3.14+ and add it to PATH.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment (venv)...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists.
)

echo Checking interpreter...
"venv\Scripts\python.exe" --version
if %errorlevel% neq 0 (
    echo Virtual environment seems broken. Delete the venv folder and run install.bat again.
    pause
    exit /b 1
)

echo.
echo Done. No third-party packages required (standard library only).
echo Run with: start.bat
pause
