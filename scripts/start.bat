@echo off
rem Starts the bot, and starts it again if it ever stops.
rem To stop the bot for good, close this window.
chcp 65001 >nul
cd /d "%~dp0.."
:loop
".venv\Scripts\python.exe" -m bot run
echo.
echo The bot stopped. Starting again in 15 seconds. Close this window to stop it.
ping -n 16 127.0.0.1 >nul
goto loop
