@echo off
cd /d %~dp0
if not exist .venv (
  echo [setup] First run: creating virtual environment and installing packages...
  python -m venv .venv || (echo Please install Python 3.10+ from https://www.python.org/downloads/ ^(check "Add python.exe to PATH"^) & pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python run.py %*
pause
