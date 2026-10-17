@echo off
REM Understudy - Windows launcher (Command Prompt).
REM Runs the first-time setup if needed (setup.bat), then starts the backend (port 8000) and the web UI (port 3000).
setlocal
cd /d "%~dp0"
set VERSION=
for /f "tokens=2 delims==" %%v in ('findstr /b /c:"WIZARD_VERSION" core\wizard\__init__.py') do set "VERSION=%%~v"
set "VERSION=%VERSION: =%"
set "VERSION=%VERSION:"=%"
title Understudy v%VERSION%

echo ================================================================================
echo   UNDERSTUDY v%VERSION%  //  Watch -^> Build -^> Govern -^> Execute -^> Export
echo ================================================================================
echo.

echo [0/2] Checking the first-run setup (skipped automatically once complete)...
call setup.bat
if errorlevel 1 (
    echo Setup failed - see the messages above.
    pause
    exit /b 1
)

echo.
echo [CHECK] Toolchains the automation tools need on THIS machine:
call :check node "Cypress, Playwright JS, Puppeteer, WebdriverIO"
call :check npx "Cypress, Playwright JS, Puppeteer, WebdriverIO"
call :check java "REST Assured, Selenium Java - Understudy can download a JDK: Mission Control - Environments"
call :check mvn "Maven builds - Understudy can download Maven: Mission Control - Access and tools"
set BROWSER_FOUND=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set BROWSER_FOUND=Google Chrome
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set BROWSER_FOUND=Google Chrome
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set BROWSER_FOUND=Google Chrome
if not defined BROWSER_FOUND if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set BROWSER_FOUND=Microsoft Edge
if not defined BROWSER_FOUND if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set BROWSER_FOUND=Microsoft Edge
if defined BROWSER_FOUND (echo    browser  found   ^(%BROWSER_FOUND% - Selenium, Robot Framework^)) else (echo    browser  not found - Selenium / Robot Framework use Playwright's Chromium via self-healing)
echo    (Every runtime Understudy finds, and what each tool needs: Mission Control ^> Environments.)

echo.
echo [CHECK] Google Cloud / Gemini (settings from .env; no network calls):
venv\Scripts\python.exe scripts\gcp_check.py --quick
echo    Set up Google Cloud: scripts\gcp_setup.bat ^<project-id^>   ^|   full live check: venv\Scripts\python.exe scripts\gcp_check.py

REM Mission Control password: generated + saved in .env on the first start (shown once), reused afterwards - like Cloud Run.
echo.
venv\Scripts\python.exe scripts\admin_password.py ensure

REM Free the ports if an earlier run is still listening.
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000,3000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" >nul 2>nul

echo.
echo [1/2] Starting the backend + demo ERP on http://localhost:8000 ...
start "Understudy Backend (8000)" /B venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000

REM Wait (max 60 s) until the backend answers.
powershell -NoProfile -Command "for ($i=0; $i -lt 120; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:8000/api/health | Out-Null; exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1" >nul 2>nul
if errorlevel 1 echo [WARN] The backend did not answer within 60 s - see the messages above.

echo [2/2] Starting the web UI on http://localhost:3000 ...
pushd understudy-ui
start "Understudy UI (3000)" /B npm.cmd start -- -p 3000
popd
timeout /t 4 /nobreak >nul

echo.
echo ================================================================================
echo   UNDERSTUDY v%VERSION% IS RUNNING
echo ================================================================================
echo   Understudy Studio        -^>  http://localhost:3000/wizard   (accounts: Mission Control - Users, or Sign up)
echo   Mission Control (admin)  -^>  http://localhost:3000
echo      login: admin / your saved password   (show it: venv\Scripts\python.exe scripts\admin_password.py show)
echo   Demo ERP portals         -^>  http://localhost:8000/portal1  /portal2  /portal3
echo   Email codes (no SMTP)    -^>  python scripts\mail_outbox.py   (signup, password reset, two-step sign-in)
echo   Google Cloud Run deploy  -^>  deploy_cloudrun.bat ^<project-id^>   (the hosted submission: Gemini on Vertex AI + Firestore)
echo   Backend API docs         -^>  http://localhost:8000/docs
echo ================================================================================
echo Press any key to stop both servers...
pause >nul

powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000,3000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" >nul 2>nul
echo Understudy stopped.
exit /b 0

:check
where %1 >nul 2>nul
if errorlevel 1 (echo    %~1	MISSING ^(%~2^)) else (echo    %~1	found   ^(%~2^))
exit /b 0
