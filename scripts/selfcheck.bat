@echo off
rem Checks the configuration and every connection. Changes nothing.
chcp 65001 >nul
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m bot selfcheck
echo.
pause
