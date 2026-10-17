#!/bin/sh
# Understudy - all-in-one container entrypoint (Dockerfile): backend on 8000, web UI on 3000.
set -e
cd /app
VERSION="$(sed -n 's/^WIZARD_VERSION = "\([^"]*\)".*/\1/p' core/wizard/__init__.py)"

echo "================================================================================"
echo "  UNDERSTUDY v${VERSION}  //  all-in-one container"
echo "================================================================================"

# Mission Control password: from the environment (docker compose passes .env), else generated once and saved in
# /app/.env inside the container (shown once) - the same rule as run_demo and Cloud Run.
python scripts/admin_password.py ensure || true

echo "[ENTRYPOINT] Starting the backend on http://0.0.0.0:8000 (its status banner follows)..."
uvicorn server:app --host 0.0.0.0 --port 8000 &
BACKEND=$!

# wait (max 60 s) until the backend answers
i=0
until curl -fs http://127.0.0.1:8000/api/health >/dev/null 2>&1; do
  i=$((i+1)); [ $i -ge 120 ] && { echo "[ENTRYPOINT] the backend did not start"; exit 1; }
  kill -0 $BACKEND 2>/dev/null || { echo "[ENTRYPOINT] the backend exited"; exit 1; }
  sleep 0.5
done

echo "[ENTRYPOINT] Starting the web UI on http://0.0.0.0:3000"
echo "  Understudy Studio        ->  http://localhost:3000/wizard   (accounts: Mission Control > Users, or Sign up)"
echo "  Mission Control (admin)  ->  http://localhost:3000"
echo "     login: admin / your saved password   (show it: docker exec <container> python scripts/admin_password.py show)"
echo "  Demo ERP portals         ->  http://localhost:8000/portal1  /portal2  /portal3"
echo "  Email codes (no SMTP)    ->  python scripts/mail_outbox.py   (signup, password reset, two-step sign-in)"
echo "  Google Cloud Run deploy  ->  ./deploy_cloudrun.sh <project-id>   (the hosted submission: Gemini on Vertex AI + Firestore)"
cd /app/understudy-ui
exec npm run start -- -p 3000 -H 0.0.0.0
