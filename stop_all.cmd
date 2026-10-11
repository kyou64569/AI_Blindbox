@echo off
REM Stop AI Blind Box services (wrapper for stop.py)
REM
REM IMPORTANT: keep this file ASCII-only.
REM   cmd.exe reads batch files using the OEM codepage (936/GBK on zh-CN).
REM   UTF-8 Chinese bytes become mojibake, and the mangled text can contain
REM   bytes cmd then treats as command separators, producing errors like
REM   "'xxx' is not recognized as an internal or external command".
REM   So BOTH comments and echo text must stay pure ASCII in this file.

setlocal

REM Locate the project from this script's own directory instead of hardcoding
REM an absolute path -- otherwise moving the folder breaks the script.
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "VENV_PY=%ROOT%\backend\.venv\Scripts\python.exe"

echo Stopping AI Blind Box services...
echo.

if exist "%VENV_PY%" (
  "%VENV_PY%" "%ROOT%\stop.py"
  goto done
)

echo [WARN] venv not found: %VENV_PY%
echo Falling back to killing processes listening on the known ports.
echo.

call :killport 8000
call :killport 8001
call :killport 5173
call :killport 5174

goto done

REM Kill whatever listens on 127.0.0.1:<port>.
:killport
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "127.0.0.1:%~1" ^| findstr "LISTENING"') do (
  echo   killing PID %%p on port %~1
  taskkill /F /PID %%p >nul 2>&1
)
goto :eof

:done
echo.
pause
endlocal