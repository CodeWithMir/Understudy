@echo off
rem Understudy - deploy to Google Cloud Run from Windows. Usage: deploy_cloudrun.bat ^<project-id^> [region]
rem Runs deploy_cloudrun.ps1 (needs the Google Cloud CLI "gcloud": https://cloud.google.com/sdk/docs/install).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_cloudrun.ps1" %*
exit /b %ERRORLEVEL%
