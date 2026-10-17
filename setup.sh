#!/usr/bin/env bash
# Understudy - one-time (idempotent) bootstrap for macOS / Linux. Safe to re-run; finished steps are skipped.
# Installs: Python venv + backend dependencies (incl. the optional Google Cloud clients), Playwright Chromium,
# the web UI's dependencies and its production build.
# Automation tools (Selenium, Cypress, ...) are NOT installed here: the admin adds them in Mission Control and
# Understudy installs each one on demand in its own isolated environment.
set -e
cd "$(dirname "$0")"
VERSION="$(sed -n 's/^WIZARD_VERSION = "\([^"]*\)".*/\1/p' core/wizard/__init__.py)"

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || { echo "[ERROR] Python 3.10+ not found (https://www.python.org/downloads/)"; exit 1; }
command -v npm >/dev/null 2>&1    || { echo "[ERROR] Node.js 20.9+ / npm not found (https://nodejs.org/ - Node 24 LTS recommended)"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "[ERROR] Python 3.10 or newer is required."; exit 1; }
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>20||(a===20&&b>=9)?0:1)' \
  || { echo "[ERROR] Node.js 20.9 or newer is required (found $(node -v))."; exit 1; }

if [ ! -x venv/bin/python ]; then
  echo "[SETUP] Creating the virtual environment (venv/)..."
  "$PY" -m venv venv
fi
# The marker holds the Understudy version the dependencies were installed for, so an upgrade re-installs them once.
if [ "$(cat venv/.deps_installed 2>/dev/null)" != "$VERSION" ]; then
  echo "[SETUP] Installing Python dependencies + Playwright Chromium for Understudy $VERSION..."
  venv/bin/python -m pip install --upgrade pip >/dev/null
  # requirements-gcp.txt = requirements.txt + Firestore / Cloud Storage clients (used only when configured)
  venv/bin/python -m pip install -r requirements-gcp.txt
  venv/bin/python -m playwright install chromium
  echo "$VERSION" > venv/.deps_installed
fi

UI_MARK=understudy-ui/.next/understudy_version.txt
if [ ! -d understudy-ui/node_modules ] || [ "$(cat "$UI_MARK" 2>/dev/null)" != "$VERSION" ]; then
  echo "[SETUP] Installing the web UI's dependencies..."
  (cd understudy-ui && { npm ci --no-audit --no-fund || npm install --no-audit --no-fund; })
fi
if [ -f understudy-ui/.next/BUILD_ID ] && [ "$(cat "$UI_MARK" 2>/dev/null)" != "$VERSION" ]; then
  echo "[SETUP] Understudy was upgraded - rebuilding the web UI..."
  rm -rf understudy-ui/.next
fi
if [ ! -f understudy-ui/.next/BUILD_ID ]; then
  echo "[SETUP] Building the web UI (one time, about 1 minute)..."
  (cd understudy-ui && npm run build)
  echo "$VERSION" > "$UI_MARK"
fi

mkdir -p generated uploads skills
[ -f .env ] || { cp .env.example .env; echo "[SETUP] Created .env from .env.example (all settings optional)."; }
echo "[SETUP] Done (Understudy $VERSION). Start it with:  ./run_demo.sh"
echo "        Connect Google Cloud (Vertex AI, Firestore, Cloud Storage):  ./scripts/gcp_setup.sh <project-id>"
