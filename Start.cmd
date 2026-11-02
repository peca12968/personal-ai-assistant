@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m assistant
if errorlevel 1 goto failed
exit /b 0
:failed
echo Assistant failed to start. Check Python and Codex installation.
pause
exit /b 1
