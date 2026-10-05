@echo off
title Crop Insurance Dashboard
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 launch_dashboard.py
) else (
    python launch_dashboard.py
)
if errorlevel 1 pause
