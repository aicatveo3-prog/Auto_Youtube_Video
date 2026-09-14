@echo off
REM First upload. The remote is already linked to:
REM   https://github.com/aicatveo3-prog/Auto_Youtube_Video.git
REM A GitHub login window will pop up the first time. Log in as the
REM account that OWNS that repo (aicatveo3-prog).
cd /d "%~dp0"

echo.
echo  Uploading to GitHub. A login window may appear - finish it there.
echo.

git push -u origin main

echo.
if errorlevel 1 (
  echo  [!] Failed.
  echo      If it says "Permission denied" or "403", you are logged in as
  echo      the WRONG GitHub account. Fix it like this:
  echo        1) Open "Credential Manager" in Windows
  echo        2) Windows Credentials -^> find git:https://github.com
  echo        3) Remove it, then run this file again and log in as aicatveo3-prog
) else (
  echo  [OK] Done. New captions will upload automatically from now on.
)
pause
