@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" scripts\publish.py
if errorlevel 1 pause
exit /b
:failed
echo Could not prepare the project Python environment.
pause
exit /b 1
