@echo off
setlocal
title Dezignatek Plant Simulator
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem Double-click to run the Plant Simulator. No terminal knowledge needed.
rem
rem   * Portable ZIP build (has a "runtime" folder next to this file): uses the
rem     bundled Python, nothing to install.
rem   * Source checkout: uses the Python installed on this PC, keeps the
rem     packages in a private ".venv" folder here (created on first run, needs
rem     internet once).
rem
rem Leave the window open while you use the simulator; close it to stop.
rem ---------------------------------------------------------------------------

set "PY="
if exist "%~dp0runtime\python.exe" set "PY=%~dp0runtime\python.exe"
if defined PY goto :run

rem -- source checkout: find a Python and set up the private environment --
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
    goto :run
)
set "SYSPY="
where py >nul 2>nul
if not errorlevel 1 set "SYSPY=py -3"
if not defined SYSPY (
    where python >nul 2>nul
    if not errorlevel 1 set "SYSPY=python"
)
if not defined SYSPY (
    echo.
    echo Python was not found on this computer.
    echo.
    echo Either install Python 3.9 or newer from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH" in the installer^), then double-click this file again,
    echo or ask for the portable ZIP build of the simulator, which needs nothing installed.
    echo.
    pause
    exit /b 1
)
echo First run - setting up a private Python environment for the simulator (about a minute)...
%SYSPY% -m venv "%~dp0.venv"
if errorlevel 1 goto :fail
set "PY=%~dp0.venv\Scripts\python.exe"

:run
rem -- make sure the packages are there (one-off; needs internet) --
"%PY%" -c "import streamlit, pandas, altair" >nul 2>nul
if errorlevel 1 (
    echo Installing the packages the simulator needs - one-off, needs internet...
    "%PY%" -m pip install --disable-pip-version-check --no-warn-script-location -r "%~dp0requirements.txt"
    if errorlevel 1 goto :fail
)

echo.
"%PY%" "%~dp0launch.py"
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Something went wrong - the messages above say what. Take a photo of this window if you need help.
echo.
pause
exit /b 1
