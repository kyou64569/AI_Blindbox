@echo off
REM Build the AI Blind Box desktop exe. Keep ASCII-only (cmd.exe uses GBK codepage).
REM
REM Usage:
REM   build.cmd            -> one-dir   desktop\dist\AIBlindBox\AIBlindBox.exe  (fast, recommended)
REM   build.cmd onefile    -> one-file  desktop\dist\AIBlindBox.exe
REM   build.cmd all        -> both
REM
REM Why "all" exists: dist holds BOTH artifacts, and each one embeds its own copy
REM of frontend\demo.html at build time. Rebuilding only one of them leaves the
REM other showing the OLD UI -- which looks like "my changes did not take effect".
REM This script therefore prints a freshness report at the end (check_artifacts.py).
setlocal
REM Resolve the parent of this script into a clean absolute path, so the summary
REM below prints "D:\work\AI_Blindbox\desktop\dist\..." instead of "..\desktop\..".
REM (Never put a percent-tilde expansion inside a REM line: cmd still parses it.)
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PY=%ROOT%\backend\.venv\Scripts\python.exe"
set "SPEC=%ROOT%\desktop\build.spec"
set "DIST=%ROOT%\desktop\dist"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=onedir"
REM Backwards compatibility with the old switch.
if "%BLINDBOX_ONEFILE%"=="1" if /I "%MODE%"=="onedir" set "MODE=onefile"

if /I "%MODE%"=="onedir"   goto PREPARE
if /I "%MODE%"=="onefile"  goto PREPARE
if /I "%MODE%"=="all"      goto PREPARE
echo [ERROR] unknown mode: "%MODE%"
echo Usage: build.cmd [onedir ^| onefile ^| all]
pause
exit /b 1

:PREPARE
if not exist "%PY%" (
  echo [ERROR] venv not found: %PY%
  pause
  exit /b 1
)

echo Verifying desktop dependencies (pywebview + PyInstaller)...
"%PY%" -m pip install --disable-pip-version-check -q -r "%ROOT%\desktop\requirements-desktop.txt"
if errorlevel 1 (
  echo [ERROR] failed to install desktop dependencies
  pause
  exit /b 1
)

REM app.ico is needed both as the PE icon and as the window/taskbar icon.
REM Re-run desktop\make_icon.py manually whenever the design changes.
if not exist "%ROOT%\desktop\app.ico" (
  echo Generating desktop icon...
  "%PY%" "%ROOT%\desktop\make_icon.py"
  if errorlevel 1 (
    echo [ERROR] failed to generate desktop\app.ico
    pause
    exit /b 1
  )
)

REM Startup animation frames (transparent RGBA PNGs shown by the native splash).
REM Re-run desktop\make_splash_frames.py manually whenever the animation changes.
if not exist "%ROOT%\desktop\splash_frames\manifest.json" (
  echo Generating startup animation frames...
  "%PY%" "%ROOT%\desktop\make_splash_frames.py"
  if errorlevel 1 (
    echo [ERROR] failed to generate desktop\splash_frames
    pause
    exit /b 1
  )
)

if /I "%MODE%"=="onedir"  goto BUILD_ONEDIR
if /I "%MODE%"=="onefile" goto BUILD_ONEFILE
goto BUILD_ONEDIR

:BUILD_ONEDIR
echo.
echo Building ONE-DIR exe...
REM Build into a scratch distpath, then publish with move. PyInstaller's COLLECT
REM stage deletes the target folder before writing into it; when that folder
REM already holds ~200 files the sandbox recycle-bin guard refuses the bulk
REM delete and leaves a HALF-DELETED build behind. Building fresh + swapping
REM sidesteps the delete entirely.
"%PY%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%\desktop\dist_onedir" --workpath "%ROOT%\desktop\build_onedir" "%SPEC%"
if errorlevel 1 (
  echo [ERROR] PyInstaller build failed. See output above.
  pause
  exit /b 1
)
if not exist "%DIST%" mkdir "%DIST%"
REM Free the publish slot. rd works on a normal desktop; if a guard refuses it,
REM move the old build aside instead so the swap can still complete.
if exist "%DIST%\AIBlindBox" rd /s /q "%DIST%\AIBlindBox" 2>nul
if exist "%DIST%\AIBlindBox" (
  echo [WARN] old one-dir build could not be deleted, moving it aside...
  move /y "%DIST%\AIBlindBox" "%DIST%\_stale_AIBlindBox_%RANDOM%" >nul
)
move /y "%ROOT%\desktop\dist_onedir\AIBlindBox" "%DIST%\AIBlindBox" >nul
if errorlevel 1 (
  echo [ERROR] failed to publish "%DIST%\AIBlindBox"
  pause
  exit /b 1
)
rd /s /q "%ROOT%\desktop\dist_onedir" 2>nul
rd /s /q "%ROOT%\desktop\build_onedir" 2>nul
echo Done: "%DIST%\AIBlindBox\AIBlindBox.exe"
if /I "%MODE%"=="all" goto BUILD_ONEFILE
goto ARTIFACT_CHECK

:BUILD_ONEFILE
echo.
echo Building SINGLE-FILE exe...
set "BLINDBOX_ONEFILE=1"
REM Build into a scratch distpath, then publish with move. Letting PyInstaller
REM overwrite the existing dist\AIBlindBox.exe directly can be refused by the
REM sandbox recycle-bin guard (the old exe would silently survive).
"%PY%" -m PyInstaller --noconfirm --clean --distpath "%ROOT%\desktop\dist_onefile" --workpath "%ROOT%\desktop\build_onefile" "%SPEC%"
if errorlevel 1 (
  echo [ERROR] PyInstaller build failed. See output above.
  pause
  exit /b 1
)
if not exist "%DIST%" mkdir "%DIST%"
move /y "%ROOT%\desktop\dist_onefile\AIBlindBox.exe" "%DIST%\AIBlindBox.exe" >nul
if errorlevel 1 (
  echo [ERROR] failed to publish dist\AIBlindBox.exe
  pause
  exit /b 1
)
rd /s /q "%ROOT%\desktop\dist_onefile" 2>nul
rd /s /q "%ROOT%\desktop\build_onefile" 2>nul
echo Done: "%DIST%\AIBlindBox.exe"
goto ARTIFACT_CHECK

:ARTIFACT_CHECK
echo.
echo ===============================================================
echo  Artifact freshness (does the exe carry the current demo.html?)
echo ===============================================================
"%PY%" "%ROOT%\desktop\check_artifacts.py"
echo.
echo ===============================================================
echo  Double-click the exe marked CURRENT above.
echo    one-dir : "%DIST%\AIBlindBox\AIBlindBox.exe"
echo    one-file: "%DIST%\AIBlindBox.exe"
echo  Anything marked STALE is an old build and will show the OLD UI.
echo  The web version (start_all.cmd) is unchanged.
echo ===============================================================
pause
exit /b 0
