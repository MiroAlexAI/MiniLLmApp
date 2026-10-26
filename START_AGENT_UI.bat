@echo off
rem Web UI for the builder agent: http://127.0.0.1:8090
cd /d "%~dp0.."
start "" http://127.0.0.1:8090
python builder\webui.py
pause
