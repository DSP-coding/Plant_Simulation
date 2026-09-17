@echo off
rem Double-click to build the portable ZIP (see build_portable.ps1 in this folder). Needs internet once.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_portable.ps1"
echo.
pause
