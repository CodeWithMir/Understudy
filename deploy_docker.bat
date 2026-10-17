@echo off
REM Understudy - build and start everything in Docker (backend + web UI).
REM   deploy_docker.bat     (= docker compose up --build -d, plus Google Cloud credentials when .env asks for them)
REM Stop with: docker compose down
setlocal
cd /d "%~dp0"
set VERSION=
for /f "tokens=2 delims==" %%v in ('findstr /b /c:"WIZARD_VERSION" core\wizard\__init__.py') do set "VERSION=%%~v"
set "VERSION=%VERSION: =%"
set "VERSION=%VERSION:"=%"
REM image tags in docker-compose.yml
set "UNDERSTUDY_VERSION=%VERSION%"
echo ================================================================================
echo   UNDERSTUDY v%VERSION%  //  Docker deployment
echo ================================================================================
echo.

where docker >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Docker was not found on PATH. Install and start Docker Desktop:
    echo         https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

if not exist .env copy /y .env.example .env >nul

REM Mission Control password: generated + saved in .env on the first start (shown once), reused afterwards - like Cloud Run.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ensure_admin_password.ps1
if not exist generated mkdir generated
if not exist uploads mkdir uploads
if not exist skills mkdir skills

REM --- Google Cloud: settings come from .env; credentials are mounted read-only, never baked into the image ---
set "G_VERTEX=%GOOGLE_GENAI_USE_VERTEXAI%"
set "G_STORE=%UNDERSTUDY_STORE%"
set "G_BUCKET=%UNDERSTUDY_GCS_BUCKET%"
set "G_CRED=%GOOGLE_APPLICATION_CREDENTIALS%"
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="GOOGLE_GENAI_USE_VERTEXAI" if not defined G_VERTEX set "G_VERTEX=%%~b"
    if /i "%%a"=="UNDERSTUDY_STORE" if not defined G_STORE set "G_STORE=%%~b"
    if /i "%%a"=="UNDERSTUDY_GCS_BUCKET" if not defined G_BUCKET set "G_BUCKET=%%~b"
    if /i "%%a"=="GOOGLE_APPLICATION_CREDENTIALS" if not defined G_CRED set "G_CRED=%%~b"
)
set GCP=0
if /i "%G_VERTEX%"=="true" set GCP=1
if /i "%G_STORE%"=="firestore" set GCP=1
if defined G_BUCKET set GCP=1
set "COMPOSE_FILES=-f docker-compose.yml"
set "GCP_RUN_ARGS="
if "%GCP%"=="1" (
    if not defined G_CRED set "G_CRED=%APPDATA%\gcloud\application_default_credentials.json"
)
if "%GCP%"=="1" (
    if not exist "%G_CRED%" (
        echo [ERROR] Google Cloud is switched on in .env ^(Vertex AI / Firestore / Cloud Storage^) but no credentials
        echo         file was found at: %G_CRED%
        echo         Run:  gcloud auth application-default login     ^(or scripts\gcp_setup.bat ^<project-id^>^)
        echo         or set GOOGLE_APPLICATION_CREDENTIALS in .env to a service-account key file.
        pause
        exit /b 1
    )
    set "UNDERSTUDY_GCP_CREDENTIALS_FILE=%G_CRED%"
    set "COMPOSE_FILES=-f docker-compose.yml -f docker-compose.gcp.yml"
    set "GCP_RUN_ARGS=-v "%G_CRED%:/run/secrets/gcp-credentials.json:ro" -e GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-credentials.json"
    echo [GCP] Google Cloud on: Vertex AI=%G_VERTEX%, store=%G_STORE%, bucket=%G_BUCKET%; credentials: %G_CRED% ^(read-only^)
) else (
    echo [GCP] Google Cloud not configured - Gemini API key / offline mode. To connect: scripts\gcp_setup.bat ^<project-id^>
)
echo.

echo [1/2] Building and starting the containers (first build takes a few minutes)...
docker compose %COMPOSE_FILES% up --build -d
if errorlevel 1 (
    echo.
    echo [FALLBACK] docker compose failed - building the single all-in-one image ^(Dockerfile^)...
    docker build -t understudy:%VERSION% .
    docker rm -f understudy-app >nul 2>nul
    docker run -d -p 3000:3000 -p 8000:8000 --env-file .env %GCP_RUN_ARGS% -v "%cd%\generated:/app/generated" -v "%cd%\uploads:/app/uploads" -v "%cd%\skills:/app/skills" --name understudy-app understudy:%VERSION%
)

echo [2/2] Done. The backend prints its status banner in:  docker compose logs understudy-backend
echo.
echo ================================================================================
echo   Understudy Studio        -^>  http://localhost:3000/wizard   (accounts: Mission Control - Users, or Sign up)
echo   Mission Control (admin)  -^>  http://localhost:3000
echo      login: admin / your saved password   (show it: findstr UNDERSTUDY_ADMIN_PASSWORD .env)
echo   Demo ERP portals         -^>  http://localhost:8000/portal1  /portal2  /portal3
echo   Backend API docs         -^>  http://localhost:8000/docs
echo   Google Cloud check:       docker compose exec understudy-backend python scripts/gcp_check.py
echo   Hosted on Google Cloud:   deploy_cloudrun.bat ^<project-id^>   (Cloud Run - the deployment for the submission)
echo   Stop with:  docker compose down
echo ================================================================================
echo.
pause
