@echo off
REM AI Blind Box - DEV launcher (engineering version)
REM 双击本文件 = 启动「工程版」前端（Vite 实时编译 src/，支持热更新 HMR）。
REM 与 start_all.cmd 的区别：本文件先设置 DEV_MODE=1，再由 start.py 走 Vite 分支。
REM 注意：需要本机已安装 Node.js（且 node 在 PATH 中），frontend/node_modules 已存在。
REM 若 node 缺失，start.py 会自动回退到免构建版 demo.html 并打印提示。

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
