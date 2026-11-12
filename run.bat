@echo off
rem MLPT launcher for Windows.
rem   run.bat            - trade (paper / live, per config.toml)
rem   run.bat check      - test the IB Gateway connection
rem   run.bat demo       - dashboard with simulated prices
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1

if not exist .venv\Scripts\python.exe (
    echo Creating the Python environment...
    py -3 -m venv .venv || goto :error
    .venv\Scripts\python -m pip install --upgrade pip || goto :error
    .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)

if "%~1"=="" (
    .venv\Scripts\python -m mlpt run
) else (
    .venv\Scripts\python -m mlpt %*
)
goto :eof

:error
echo Setup failed. Is Python 3.11 or newer installed (https://www.python.org/downloads/)?
exit /b 1
