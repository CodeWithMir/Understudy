@echo off
REM Understudy - one-time (idempotent) bootstrap for Windows. Safe to re-run; finished steps are skipped.
REM Installs: Python venv + backend dependencies (incl. the optional Google Cloud clients), Playwright Chromium,
REM the web UI's dependencies and its production build.
REM Automation tools (Selenium, Cypress, ...) are NOT installed here: the admin adds them in Mission Control and
REM Understudy installs each one on demand in its own isolated environment.
setlocal
cd /d "%~dp0"
call :version

where python >nul 2>nul
if errorlevel 1 ( echo [ERROR] Python 3.10+ not found on PATH. Install from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & exit /b 1 )
where npm >nul 2>nul
if errorlevel 1 ( echo [ERROR] Node.js 20.9+ / npm not found on PATH. Install from https://nodejs.org/ ^(Node 24 LTS recommended^). & exit /b 1 )
python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)"
if errorlevel 1 ( echo [ERROR] Python 3.10 or newer is required. & exit /b 1 )
node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>20||(a===20&&b>=9)?0:1)"
if errorlevel 1 ( echo [ERROR] Node.js 20.9 or newer is required. & exit /b 1 )

if not exist venv\Scripts\python.exe (
  echo [SETUP] Creating the virtual environment ^(venv\^)...
  python -m venv venv
  if errorlevel 1 exit /b 1
)
REM The marker holds the Understudy version the dependencies were installed for, so an upgrade re-installs them once.
set DEPS_FOR=
if exist venv\.deps_installed set /p DEPS_FOR=<venv\.deps_installed
if not "%DEPS_FOR%"=="%VERSION%" (
  echo [SETUP] Installing Python dependencies and Playwright Chromium for Understudy %VERSION%...
  venv\Scripts\python.exe -m pip install --upgrade pip >nul
  REM requirements-gcp.txt = requirements.txt + Firestore / Cloud Storage clients (used only when configured)
  venv\Scripts\python.exe -m pip install -r requirements-gcp.txt
  if errorlevel 1 exit /b 1
  venv\Scripts\python.exe -m playwright install chromium
  if errorlevel 1 exit /b 1
  >venv\.deps_installed echo %VERSION%
)

REM The web UI: dependencies and the production build are refreshed once after an upgrade (version marker).
set UI_MARK=understudy-ui\.next\understudy_version.txt
set UI_BUILT=
if exist "%UI_MARK%" set /p UI_BUILT=<"%UI_MARK%"
set NEED_NPM=0
if not exist understudy-ui\node_modules set NEED_NPM=1
if not "%UI_BUILT%"=="%VERSION%" set NEED_NPM=1
if "%NEED_NPM%"=="1" (
  echo [SETUP] Installing the web UI's dependencies...
  pushd understudy-ui
  call npm ci --no-audit --no-fund
  if errorlevel 1 call npm install --no-audit --no-fund
  if errorlevel 1 ( popd & exit /b 1 )
  popd
)
if not "%UI_BUILT%"=="%VERSION%" if exist understudy-ui\.next\BUILD_ID (
  echo [SETUP] Understudy was upgraded - rebuilding the web UI...
  rmdir /s /q understudy-ui\.next
)
if not exist understudy-ui\.next\BUILD_ID (
  echo [SETUP] Building the web UI - one time, about 1 minute...
  pushd understudy-ui
  call npm run build
  if errorlevel 1 ( popd & exit /b 1 )
  popd
  >"%UI_MARK%" echo %VERSION%
)

if not exist generated mkdir generated
if not exist uploads mkdir uploads
if not exist skills mkdir skills
if not exist .env (
  copy /y .env.example .env >nul
  echo [SETUP] Created .env from .env.example ^(all settings optional^).
)
echo [SETUP] Done ^(Understudy %VERSION%^).
echo         Connect Google Cloud ^(Vertex AI, Firestore, Cloud Storage^):  scripts\gcp_setup.bat ^<project-id^>
exit /b 0

:version
REM Reads WIZARD_VERSION = "x.y.z" from core\wizard\__init__.py (the single source of the version).
set VERSION=
for /f "tokens=2 delims==" %%v in ('findstr /b /c:"WIZARD_VERSION" core\wizard\__init__.py') do set "VERSION=%%~v"
set "VERSION=%VERSION: =%"
set "VERSION=%VERSION:"=%"
exit /b 0
