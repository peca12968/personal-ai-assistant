@echo off
cd /d "%~dp0"
python scripts\publish.py
if errorlevel 1 pause
