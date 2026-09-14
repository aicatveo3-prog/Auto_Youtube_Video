@echo off
REM ASCII only. cmd.exe reads .bat with the OEM codepage, so non-ASCII text
REM here gets mangled. All Korean output comes from webui.py instead.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] python not found on PATH.
  pause
  exit /b 1
)

where yt-dlp >nul 2>&1
if errorlevel 1 (
  echo [ERROR] yt-dlp not found. Install with: python -m pip install yt-dlp
  pause
  exit /b 1
)

python scripts\webui.py --open

echo.
echo Server stopped.
pause
