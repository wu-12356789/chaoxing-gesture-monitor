@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import requests; from Crypto.Cipher import DES" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" src\main.py %*
set "gestureExit=%errorlevel%"
pause
exit /b %gestureExit%
:failed
echo Setup failed. See the error above.
pause
exit /b 1
