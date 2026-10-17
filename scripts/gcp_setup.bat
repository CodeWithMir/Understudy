@echo off
REM Understudy - connect this installation to Google Cloud (Vertex AI, Firestore, Cloud Storage).
REM Usage: scripts\gcp_setup.bat ^<project-id^> [region]
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0gcp_setup.ps1" %*
if errorlevel 1 pause
