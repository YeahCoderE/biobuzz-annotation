@echo off
cd /d "%~dp0"
start "" "http://127.0.0.1:8767/?static=1"
python -m http.server 8767 --directory docs
if errorlevel 1 pause
