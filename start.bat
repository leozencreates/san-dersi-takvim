@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
chcp 65001 >nul
python bot.py
pause
