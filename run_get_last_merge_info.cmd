@echo off
REM Runs get_last_merge_info.py using the input.csv, git_login.json, and
REM output.csv files in this same folder. Double-click this file, or run it
REM from a command prompt.

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python was not found on PATH. Install Python 3 and try again.
    pause
    exit /b 1
)

python get_last_merge_info.py --input input.csv --creds git_login.json --output output.csv

if errorlevel 1 (
    echo.
    echo Script exited with an error - see messages above.
) else (
    echo.
    echo Done. See output.csv for results.
)

pause
