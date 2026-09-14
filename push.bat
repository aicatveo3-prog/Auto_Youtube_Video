@echo off
REM Manual upload: use only if you edited files by hand or auto-upload failed.
REM Normal collection through the web UI uploads itself.
cd /d "%~dp0"

git add -A
git commit -m "manual upload"
git push

echo.
if errorlevel 1 (
  echo Nothing to upload, or push failed. See messages above.
) else (
  echo [OK] Uploaded.
)
pause
