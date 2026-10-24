@echo off
title Real View CRM - DEMO
rem Opens the CRM with sample data in a separate demo database (instance\demo.db).
rem Your real data (instance\crm.db) is not touched.
cd /d "%~dp0"

set PY=
python --version >nul 2>&1 && set PY=python
if not defined PY py --version >nul 2>&1 && set PY=py
if not defined PY (
  echo.
  echo  Python is not installed. Install it from https://www.python.org/downloads/
  echo  and tick "Add Python to PATH" on the first screen. Then run demo.bat again.
  pause
  exit /b
)

echo Installing / checking required files (first time takes a minute)...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
set CRM_DB_PATH=%~dp0instance\demo.db
echo Preparing demo data...
%PY% seed_demo.py

echo.
echo  ================================================================
echo   Real View CRM DEMO is running:  http://localhost:5000
echo   All demo passwords:  demo1234
echo     superadmin      Super Admin (Real View Creative Studio)
echo     abc_admin       Admin of client "ABC Motors"
echo     abc_staff1      Staff of ABC Motors (Anu Thomas)
echo     abc_staff2      Staff of ABC Motors (Ben Joseph)
echo     green_admin     Admin of client "Green Homes Realty"
echo     green_staff1    Staff of Green Homes Realty
echo     cafe_admin      Admin of cafe client "Chai Corner" (billing, menu, QR)
echo     cafe_cashier    Cashier of Chai Corner
echo   Digital menu:  http://localhost:5000/m/chai
echo   KEEP THIS WINDOW OPEN. Close it to stop.
echo   To start the demo again from scratch, delete instance\demo.db
echo  ================================================================
echo.
start "" cmd /c "timeout /t 3 >nul & start http://localhost:5000"
%PY% app.py
pause
