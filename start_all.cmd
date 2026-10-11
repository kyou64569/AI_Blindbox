@echo off
REM AI Blind Box - one-click launcher
REM NOTE: keep this file ASCII-only (cmd.exe uses GBK codepage)

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set VENV_PY=%ROOT%\backend\.venv\Scripts\python.exe

if not exist "%VENV_PY%" (
  echo [ERROR] venv not found: %VENV_PY%
  echo Please create venv and install deps first.
  echo See README.md for instructions.
  pause
  exit /b 1
)

title AI Blind Box

echo.
echo ================================================
echo   AI Blind Box Launcher
echo ================================================
echo.
echo Starting services. This may take up to 20 seconds.
echo Look for "AI Blind Box" on your taskbar for this window.
echo The actual URL will be printed below and opened automatically.
echo   (port 5173 is used unless it's already taken, then 5174, 5175...)
echo Press Ctrl+C to stop all services at any time.
echo.

"%VENV_PY%" "%ROOT%\start.py"
set RC=%errorlevel%

echo.
if %RC% neq 0 (
  echo [ERROR] start.py exited with code %RC%
  echo Check log files in: %ROOT%\.cache\
  echo   backend.log  - backend (uvicorn) output
  echo   frontend.log - frontend (serve.py) output
)
pause
