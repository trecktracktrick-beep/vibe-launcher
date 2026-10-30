@echo off
title Vibe Launcher
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py launcher.py
) else (
  python launcher.py
)
if errorlevel 1 pause
