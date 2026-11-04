@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import PIL, pypdf; assert PIL.__version__ == '13.0.0' and pypdf.__version__ == '6.21.4'" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m scripts.daily_chat %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Assistant failed to start. Check Python and Codex installation.
pause
exit /b 1
