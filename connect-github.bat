@echo off
REM One-time setup: link this folder to your GitHub repo and push everything.
REM ASCII only on purpose (cmd mangles non-ASCII .bat lines).
cd /d "%~dp0"

echo.
echo  ================================================================
echo   GitHub 1-time connect
echo  ----------------------------------------------------------------
echo   1) Create an EMPTY repo at github.com (no README).
echo      Private is recommended.
echo   2) Copy its URL, e.g.
echo         https://github.com/yourname/your-repo.git
echo   3) Paste it below and press Enter.
echo  ================================================================
echo.

set /p URL="Repo URL: "
if "%URL%"=="" (
  echo No URL entered. Aborting.
  pause
  exit /b 1
)

git remote remove origin 2>nul
git remote add origin %URL%
git branch -M main
echo.
echo Pushing... a browser may open for GitHub login the first time.
git push -u origin main

echo.
if errorlevel 1 (
  echo [!] Push failed. Check the URL and your GitHub login, then retry.
) else (
  echo [OK] Uploaded. From now on new captions upload automatically.
)
pause
