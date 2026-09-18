@echo off
REM ASCII only. cmd.exe reads .bat with the OEM codepage, so non-ASCII text
REM here gets mangled. Korean output comes from the Python scripts instead.
REM
REM What this does: reads every transcripts\<id>\shots.json (written by the
REM online AI), grabs those frames with yt-dlp + ffmpeg, rebuilds the site,
REM and uploads. Run this by hand whenever the AI has added new capture lists.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set GIT_TERMINAL_PROMPT=0

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] python not found on PATH.
  pause
  exit /b 1
)
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [ERROR] ffmpeg not found on PATH. Frame capture cannot run.
  echo         Install ffmpeg and add its bin folder to PATH.
  pause
  exit /b 1
)

echo === 1/5  Capturing frames from shots.json ===
python scripts\ytframes.py sync
if errorlevel 1 (
  echo [ERROR] Frame capture failed. See messages above.
  pause
  exit /b 1
)

echo.
echo === 2/5  Committing captured frames ===
git add -A
git commit -m "capture: frames"

echo.
echo === 3/5  Syncing with GitHub ===
git fetch origin
git rebase origin/main
if errorlevel 1 (
  git rebase --abort
  echo [ERROR] Remote has other changes. Run push.bat or try again.
  pause
  exit /b 1
)

echo.
echo === 4/5  Rebuilding site ===
python scripts\build_site.py

echo.
echo === 5/5  Uploading ===
git add -A
git commit -m "site: frames rebuild [skip ci]"
git push

echo.
if errorlevel 1 (
  echo Nothing new to upload, or push failed. See messages above.
) else (
  echo [OK] Frames captured and uploaded.
)
pause
