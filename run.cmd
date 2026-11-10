@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul && (python l3250_clean.py & goto :eof)
where py >nul 2>nul && (py l3250_clean.py & goto :eof)
echo Python not found. Install Python 3.10+ and tick "Add python.exe to PATH".
pause
