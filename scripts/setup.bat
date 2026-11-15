@echo off
rem One-time setup: creates the virtual environment and installs the packages.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [X] Could not create the virtual environment. Is Python 3.12 installed?
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [X] Package installation failed. See the messages above.
    pause
    exit /b 1
)
if not exist ".env" copy ".env.example" ".env" >nul
echo.
echo [OK] Setup finished. Next: fill in the .env file, then run scripts\selfcheck.bat
pause
