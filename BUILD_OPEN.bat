@echo off
rem Build the last builder project (parts -> dist\index.html) and open it in the browser.
cd /d "%~dp0.."
python builder\build.py --open
if errorlevel 1 pause
