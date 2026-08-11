@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === RAID / DUMP Control Panel ===
echo Folder: %CD%
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo Python not found in PATH
    pause
    exit /b 1
)

if not exist "control_panel.py" (
    echo control_panel.py not found
    pause
    exit /b 1
)

python -u control_panel.py
if errorlevel 1 (
    echo.
    echo Panel exited with error
    pause
)
