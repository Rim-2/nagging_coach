@echo off
chcp 65001 >nul
title Nagging Coach
cd /d "%~dp0"
"C:\Program Files\Python310\python.exe" -u app.py
echo.
echo [App stopped - check the log above, then press any key to close]
pause >nul
