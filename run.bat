@echo off
REM FaceMark - AI Attendance System
REM Windows startup script

echo.
echo ============================================================
echo   FaceMark - AI Attendance System
echo   YuNet detection (MIT) + SFace recognition (Apache-2.0)
echo   Opening: http://127.0.0.1:8000
echo ============================================================
echo.

REM Check if virtual environment exists
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
    echo Installing dependencies...
    .venv\Scripts\pip install -r requirements.txt
)

REM Activate venv and start server
call .venv\Scripts\activate.bat
python run.py

pause