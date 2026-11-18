@echo off
REM AI Blind Box desktop launcher (POC: reuses the existing backend venv)
REM Keep this file ASCII-only (cmd.exe uses GBK codepage).
set "ROOT=%~dp0.."
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] venv not found: %PY%
  pause
  exit /b 1
)

title AI Blind Box Desktop
"%PY%" "%ROOT%\desktop\launcher.py" %*
set RC=%errorlevel%

if not "%RC%"=="0" (
  echo [ERROR] launcher exited with code %RC%
  echo Check desktop.log and service.log in the data directory.
  pause
)
