@echo off
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\ddr-pose\venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" tools\make_package.py
pause
