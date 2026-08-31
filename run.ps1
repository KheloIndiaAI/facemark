#!/usr/bin/env pwsh
<#
.SYNOPSIS
    FaceMark - AI Attendance System startup script for Windows PowerShell

.DESCRIPTION
    Starts the FaceMark attendance system with YOLO11s-face detection
    and ArcFace ensemble recognition on http://127.0.0.1:8000
#>

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  FaceMark - AI Attendance System" -ForegroundColor Green
Write-Host "  YOLO11s-face detection + ArcFace ensemble recognition" -ForegroundColor Gray
Write-Host "  Opening: http://127.0.0.1:8000" -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check if virtual environment exists
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
    Write-Host "Installing dependencies..." -ForegroundColor Yellow
    & .venv\Scripts\pip install -r requirements.txt
}

# Activate venv and start server
Write-Host "Starting server..." -ForegroundColor Green
& .venv\Scripts\python.exe run.py