@echo off
rem Downloads the newest code and installs any new packages.
cd /d "%~dp0.."
git pull
if errorlevel 1 (
    echo.
    echo [X] git pull failed. See the messages above.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo [OK] Updated. Close the bot window and run scripts\start.bat again.
pause
