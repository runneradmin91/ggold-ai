@echo off
cd /d %~dp0
py -3.11 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist .env copy .env.example .env
echo Edit .env and ensure MT5 terminal is installed and logged in.
echo For local testing only, this binds to 127.0.0.1. Use HTTPS reverse proxy for remote access.
uvicorn main:app --host 127.0.0.1 --port 8000
pause
