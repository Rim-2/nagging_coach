@echo off
chcp 65001 >nul
title 잔소리 코치
cd /d "%~dp0"
python -u app.py
echo.
echo [앱이 종료되었습니다 - 위 로그를 확인하고 아무 키나 누르세요]
pause >nul
