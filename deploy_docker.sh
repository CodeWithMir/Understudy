#!/usr/bin/env bash
# Understudy - build and start everything in Docker (backend + web UI).
#   ./deploy_docker.sh            (= docker compose up --build -d, plus Google Cloud credentials when .env asks for them)
# Stop with: docker compose down
set -e
cd "$(dirname "$0")"
VERSION="$(sed -n 's/^WIZARD_VERSION = "\([^"]*\)".*/\1/p' core/wizard/__init__.py)"
export UNDERSTUDY_VERSION="$VERSION"          # image tags in docker-compose.yml

echo "================================================================================"
echo "  UNDERSTUDY v${VERSION}  //  Docker deployment"
echo "================================================================================"

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERROR] Docker is not installed or not on PATH. Install Docker Desktop / Docker Engine first."
  exit 1
fi

[ -f .env ] || cp .env.example .env

# Mission Control password: generated + saved in .env on the first start (shown once), reused afterwards - like Cloud Run.
if [ -z "${UNDERSTUDY_ADMIN_PASSWORD:-}" ] && ! grep -qE '^UNDERSTUDY_ADMIN_PASSWORD=.+' .env; then
  ADMIN_PW="Us-$(LC_ALL=C tr -dc 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789' </dev/urandom | head -c 18 || true)-9"
  grep -vE '^UNDERSTUDY_ADMIN_PASSWORD=$' .env > .env.tmp && mv .env.tmp .env
  printf '\nUNDERSTUDY_ADMIN_PASSWORD=%s\n' "$ADMIN_PW" >> .env
  echo "================================================================================"
  echo "  FIRST START - Mission Control login (shown ONCE):"
  echo "      ${UNDERSTUDY_ADMIN_USERNAME:-admin} / $ADMIN_PW"
  echo "  Saved in .env as UNDERSTUDY_ADMIN_PASSWORD. Show it again: grep UNDERSTUDY_ADMIN_PASSWORD .env"
  echo "================================================================================"
else
  echo "[ADMIN] Mission Control login: admin / (saved in .env - show it: grep UNDERSTUDY_ADMIN_PASSWORD .env)"
fi
mkdir -p generated uploads skills

# --- Google Cloud: settings come from .env (or the shell); credentials are mounted read-only, never baked in ---
envval() {  # value of $1 from the shell, else from .env
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f .env ]; then v="$(grep -E "^(export )?$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"; fi
  printf '%s' "$v"
}
VERTEX="$(envval GOOGLE_GENAI_USE_VERTEXAI)"; STORE="$(envval UNDERSTUDY_STORE)"; BUCKET="$(envval UNDERSTUDY_GCS_BUCKET)"
COMPOSE_FILES="-f docker-compose.yml"
GCP_RUN_ARGS=""
case "$(printf '%s' "$VERTEX" | tr 'A-Z' 'a-z')" in true|1|yes|on) GCP=1 ;; *) GCP=0 ;; esac
[ "$STORE" = "firestore" ] && GCP=1
[ -n "$BUCKET" ] && GCP=1
if [ $GCP = 1 ]; then
  CRED="$(envval GOOGLE_APPLICATION_CREDENTIALS)"
  [ -z "$CRED" ] && CRED="${CLOUDSDK_CONFIG:-$HOME/.config/gcloud}/application_default_credentials.json"
  if [ ! -f "$CRED" ]; then
    echo "[ERROR] Google Cloud is switched on in .env (Vertex AI / Firestore / Cloud Storage) but no credentials"
    echo "        file was found at: $CRED"
    echo "        Run:  gcloud auth application-default login     (or ./scripts/gcp_setup.sh <project-id>)"
    echo "        or set GOOGLE_APPLICATION_CREDENTIALS in .env to a service-account key file."
    exit 1
  fi
  export UNDERSTUDY_GCP_CREDENTIALS_FILE="$CRED"
  COMPOSE_FILES="-f docker-compose.yml -f docker-compose.gcp.yml"
  GCP_RUN_ARGS="-v $CRED:/run/secrets/gcp-credentials.json:ro -e GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-credentials.json"
  echo "[GCP] Google Cloud on: Vertex AI=${VERTEX:-false}, store=${STORE:-local}, bucket=${BUCKET:-none}; credentials: $CRED (read-only)"
else
  echo "[GCP] Google Cloud not configured - Gemini API key / offline mode. To connect: ./scripts/gcp_setup.sh <project-id>"
fi

echo "[1/2] Building and starting the containers (the first build takes a few minutes)..."
if docker compose version >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  docker compose $COMPOSE_FILES up --build -d
elif docker-compose version >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  docker-compose $COMPOSE_FILES up --build -d
else
  echo "[FALLBACK] docker compose not found - building the single all-in-one image (Dockerfile)..."
  docker build -t "understudy:${VERSION}" .
  docker rm -f understudy-app >/dev/null 2>&1 || true
  # shellcheck disable=SC2086
  docker run -d -p 3000:3000 -p 8000:8000 --env-file .env $GCP_RUN_ARGS \
    -v "$PWD/generated:/app/generated" -v "$PWD/uploads:/app/uploads" -v "$PWD/skills:/app/skills" \
    --name understudy-app "understudy:${VERSION}"
fi

echo "[2/2] Done. The backend prints its status banner in:  docker compose logs understudy-backend"
echo ""
echo "================================================================================"
echo "  Understudy Studio        ->  http://localhost:3000/wizard   (accounts: Mission Control > Users, or Sign up)"
echo "  Mission Control (admin)  ->  http://localhost:3000"
echo "     login: admin / your saved password   (show it: grep UNDERSTUDY_ADMIN_PASSWORD .env)"
echo "  Demo ERP portals         ->  http://localhost:8000/portal1  /portal2  /portal3"
echo "  Backend API docs         ->  http://localhost:8000/docs"
echo "  Google Cloud check:       docker compose exec understudy-backend python scripts/gcp_check.py"
echo "  Hosted on Google Cloud:   ./deploy_cloudrun.sh <project-id>   (Cloud Run - the deployment for the submission)"
echo "  Stop with:  docker compose down"
echo "================================================================================"
