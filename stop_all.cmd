@echo off
REM Stop AI Blind Box services (wrapper for stop.py)
REM NOTE: keep this file ASCII-only (GBK codepage).

set ROOT=D:\work\AI_Blindbox
set VENV_PY=%ROOT%\backend\.venv\Scripts\python.exe

if not exist "%VENV_PY%" (
  echo [ERROR] venv not found: %VENV_PY%
  echo Trying taskkill on known port PIDs instead...
)

echo Stopping AI Blind Box services...
echo.

"%VENV_PY%" "%ROOT%\stop.py"

echo.
pause
