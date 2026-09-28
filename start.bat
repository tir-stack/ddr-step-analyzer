@echo off
chcp 65001 >nul
title DDR Step Analyzer
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PY=%LOCALAPPDATA%\ddr-pose\venv\Scripts\python.exe
if not exist "%PY%" set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
  where python >nul 2>nul && (set PY=python) || (
    echo セットアップがまだ終わっていません。先に setup.bat をダブルクリックしてください。
    echo Setup has not been run yet. Please double-click setup.bat first.
    pause
    exit /b 1
  )
)
"%PY%" server.py %*
if errorlevel 1 pause
