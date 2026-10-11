@echo off
REM Frontend supervisor: restarts serve.py on exit (with delay).
REM Prefer start.py (unified launcher, single terminal).

REM Project root derived from this script's own directory (no hardcoded drive
REM letter, so the folder can be moved or deployed elsewhere).
REM NOTE: keep this file ASCII-only -- cmd.exe parses it with the OEM codepage
REM (936/GBK on zh-CN) and UTF-8 Chinese would turn into mojibake commands.
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set VENV_PY=%ROOT%\backend\.venv\Scripts\python.exe
set CACHE=%ROOT%\.cache
if not exist "%CACHE%" mkdir "%CACHE%"

:loop
cd /d %ROOT%\frontend
echo [%date% %time%] Starting frontend (port 5173)...
%VENV_PY% serve.py >> "%CACHE%\frontend.log" 2>&1
echo [%date% %time%] Frontend exited (code %errorlevel%), restart in 3s >> "%CACHE%\frontend.log"
echo [%date% %time%] Frontend exited (code %errorlevel%) -- see log: %CACHE%\frontend.log
ping -n 4 127.0.0.1 >nul
goto loop
