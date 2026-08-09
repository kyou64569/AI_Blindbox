@echo off
REM Backend supervisor: restarts uvicorn on exit (with delay).
REM Prefer start.py (unified launcher, single terminal).

REM 项目根基于本脚本所在目录推导（不写死盘符，便于迁移/他人部署）
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set VENV_PY=%ROOT%\backend\.venv\Scripts\python.exe
set CACHE=%ROOT%\.cache
if not exist "%CACHE%" mkdir "%CACHE%"

:loop
cd /d %ROOT%\backend
echo [%date% %time%] Starting backend (port 8000)...
%VENV_PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> "%CACHE%\backend.log" 2>&1
echo [%date% %time%] Backend exited (code %errorlevel%), restart in 5s >> "%CACHE%\backend.log"
echo [%date% %time%] Backend exited (code %errorlevel%) -- see log: %CACHE%\backend.log
ping -n 6 127.0.0.1 >nul
goto loop
