@echo off
title Antigravity Telegram AI Butler Watchdog
cd /d "%~dp0"

:loop
echo [%date% %time%] Butler ishga tushirilmoqda...
python main.py
echo [%date% %time%] Butler jarayoni to'xtadi yoki qayta yuklanmoqda. 5 soniyadan keyin avto-restart...
timeout /t 5 /nobreak >nul
goto loop
