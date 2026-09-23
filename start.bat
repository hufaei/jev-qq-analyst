@echo off
rem 启动 jev-qq-analyst Windows 悬浮面板
cd /d "%~dp0"
py -3.13 -c "import uiautomation, psutil" >nul 2>nul || py -3.13 -m pip install uiautomation psutil
py -3.13 src\hud_win.py
