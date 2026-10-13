@echo off
title Antigravity Telegram AI Butler Watchdog (Tier-4 Supervisor)
cd /d "%~dp0"

echo [%date% %time%] Antigravity Tier-4 Watchdog Supervisor ishga tushirilmoqda...
python scripts\watchdog_supervisor.py
pause

