@echo off
title Agency CRM
rem Always run from the folder this file is in
cd /d "%~dp0"

rem If this folder was set up with "git clone", fetch the latest version first.
rem Your data in the "instance" folder is never touched by updates.
if exist ".git\" (
  where git >nul 2>&1
  if not errorlevel 1 (
    echo Checking for updates...
    git pull --ff-only
    if errorlevel 1 echo Could not check for updates. Starting the current version.
    echo.
  )
)

set PY=
python --version >nul 2>&1 && set PY=python
if not defined PY py --version >nul 2>&1 && set PY=py
if not defined PY (
  echo.
  echo  Python is not installed.
  echo  1. Go to https://www.python.org/downloads/ and install Python.
  echo  2. On the first install screen, TICK "Add Python to PATH".
  echo  3. Then double-click start.bat again.
  echo.
  pause
  exit /b
)

echo Installing / checking required files (first time takes a minute)...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo  Could not install Flask. Check your internet connection and try again.
  pause
  exit /b
)

echo.
echo  ================================================
echo   Agency CRM is running.
echo   Open in your browser:  http://localhost:5000
echo   KEEP THIS WINDOW OPEN while using the CRM.
echo   To stop the CRM, close this window.
echo  ================================================
echo.
start "" cmd /c "timeout /t 3 >nul & start http://localhost:5000"
%PY% app.py
pause
