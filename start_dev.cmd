@echo off
REM AI Blind Box - DEV launcher (engineering version)
REM
REM IMPORTANT: keep this file ASCII-only. cmd.exe parses batch files with the
REM OEM codepage (936/GBK on zh-CN); UTF-8 Chinese in comments/echo becomes
REM mojibake that cmd may try to execute as commands.
REM
REM Double-click = start the "engineering" frontend (Vite compiles src/ live,
REM with HMR). Difference vs start_all.cmd: this sets DEV_MODE=1 so start.py
REM takes the Vite branch.
REM Requires: Node.js installed (node on PATH) and frontend/node_modules present.
REM If node is missing, start.py falls back to the no-build demo.html and says so.

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

set DEV_MODE=1

title AI Blind Box (DEV)
echo.
echo ================================================
echo   AI Blind Box Launcher - DEV (engineering)
echo ================================================
echo.
echo Starting services with Vite HMR (src/). Up to 20 seconds.
echo A browser will open at http://127.0.0.1:5173 when ready.
echo Press Ctrl+C to stop all services at any time.
echo.

"%VENV_PY%" "%ROOT%\start.py"
set RC=%errorlevel%

echo.
if %RC% neq 0 (
  echo [ERROR] start.py exited with code %RC%
  echo Check log files in: %ROOT%\.cache\
  echo   backend.log  - backend (uvicorn) output
  echo   frontend.log - frontend (vite) output
)
pause
