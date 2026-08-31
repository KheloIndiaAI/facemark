@echo off
REM FaceMark - AI Attendance System
REM Windows startup script

echo.
echo ============================================================
echo   FaceMark - AI Attendance System
echo   YOLO11s-face detection + ArcFace ensemble recognition
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