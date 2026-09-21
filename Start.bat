@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 scripts\start.py
) else (
  python scripts\start.py
)
if errorlevel 1 pause
