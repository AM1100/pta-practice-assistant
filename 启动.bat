@echo off
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (pause & exit /b 1)
.venv\Scripts\python app.py
pause
