#!/usr/bin/env bash
# Understudy - macOS / Linux launcher.
# Runs the first-time setup if needed (./setup.sh), then starts the backend (port 8000) and the web UI (port 3000).
#   ./run_demo.sh
# Ctrl+C stops both.
set -e
cd "$(dirname "$0")"
VERSION="$(sed -n 's/^WIZARD_VERSION = "\([^"]*\)".*/\1/p' core/wizard/__init__.py)"

echo "================================================================================"
echo "  UNDERSTUDY v${VERSION}  //  Watch -> Build -> Govern -> Execute -> Export"
echo "================================================================================"

./setup.sh

echo ""
echo "[CHECK] Toolchains the automation tools need on THIS machine:"
check() { if command -v "$1" >/dev/null 2>&1; then printf "   %-8s found   (%s)\n" "$1" "$2"; else printf "   %-8s MISSING (%s)\n" "$1" "$2"; fi; }
check node "Cypress, Playwright JS, Puppeteer, WebdriverIO"
check npx  "Cypress, Playwright JS, Puppeteer, WebdriverIO"
check java "REST Assured, Selenium Java (Understudy can download a JDK: Mission Control > Environments)"
check mvn  "Maven builds (Understudy can download Maven: Mission Control > Access & tools)"
if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 \
   || [ -d "/Applications/Google Chrome.app" ] || [ -d "/Applications/Microsoft Edge.app" ]; then
  echo "   browser  found   (Selenium, Robot Framework)"
else
  echo "   browser  not found - Selenium / Robot Framework use Playwright's Chromium via self-healing"
fi
echo "   (Every runtime Understudy finds, and what each tool needs: Mission Control > Environments.)"

echo ""
echo "[CHECK] Google Cloud / Gemini (settings from .env; no network calls):"
venv/bin/python scripts/gcp_check.py --quick || true
echo "   Set up Google Cloud: ./scripts/gcp_setup.sh <project-id>   |   full live check: venv/bin/python scripts/gcp_check.py"

# Mission Control password: generated + saved in .env on the first start (shown once), reused afterwards - like Cloud Run.
echo ""
venv/bin/python scripts/admin_password.py ensure

# Free the ports if an earlier run is still listening.
for p in 8000 3000; do
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:$p" -sTCP:LISTEN 2>/dev/null || true)"
    [ -n "$pids" ] && kill $pids 2>/dev/null || true
  elif command -v fuser >/dev/null 2>&1; then
    fuser -k "$p/tcp" >/dev/null 2>&1 || true
  fi
done

echo ""
echo "[1/2] Starting the backend + demo ERP on http://localhost:8000 ..."
venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8000 &
BACK=$!
cleanup() { kill "$BACK" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# Wait (max 60 s) until the backend answers, so the UI never starts against a cold backend.
for _ in $(seq 1 120); do
  if command -v curl >/dev/null 2>&1; then curl -fs http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break
  else venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=1)" 2>/dev/null && break; fi
  kill -0 "$BACK" 2>/dev/null || { echo "[ERROR] The backend stopped - see the messages above."; exit 1; }
  sleep 0.5
done

echo "[2/2] Starting the web UI on http://localhost:3000 ..."
echo ""
echo "================================================================================"
echo "  Understudy Studio        ->  http://localhost:3000/wizard   (accounts: Mission Control > Users, or Sign up)"
echo "  Mission Control (admin)  ->  http://localhost:3000"
echo "     login: admin / your saved password   (show it: venv/bin/python scripts/admin_password.py show)"
echo "  Demo ERP portals         ->  http://localhost:8000/portal1  /portal2  /portal3"
echo "  Email codes (no SMTP)    ->  python scripts/mail_outbox.py   (signup, password reset, two-step sign-in)"
echo "  Google Cloud Run deploy  ->  ./deploy_cloudrun.sh <project-id>   (the hosted submission: Gemini on Vertex AI + Firestore)"
echo "  Backend API docs         ->  http://localhost:8000/docs"
echo "  Press Ctrl+C to stop both servers."
echo "================================================================================"
cd understudy-ui
npm start -- -p 3000
