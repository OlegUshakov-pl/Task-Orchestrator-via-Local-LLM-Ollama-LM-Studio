@echo off
setlocal
cd /d "%~dp0"

chcp 65001 >nul 2>nul

if not exist "script.py" (
    echo ERROR: script.py not found in "%~dp0".
    pause
    exit /b 1
)

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

python script.py %*
set "CODE=%errorlevel%"
echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
