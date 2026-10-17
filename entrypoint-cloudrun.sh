#!/bin/sh
# Understudy - Cloud Run entrypoint: backend on 127.0.0.1:8000 (private), web UI on $PORT (public).
set -e
PORT="${PORT:-8080}"
echo "[understudy] Cloud Run revision ${K_REVISION:-local} - AI: $( [ "${GOOGLE_GENAI_USE_VERTEXAI}" = "true" ] && echo "Vertex AI (${GOOGLE_CLOUD_PROJECT:-?}/${GOOGLE_CLOUD_LOCATION:-us-central1})" || echo "Gemini API key" ) - state: ${UNDERSTUDY_STORE:-local}"
if [ -n "${UNDERSTUDY_ADMIN_PASSWORD:-}" ]; then
  echo "[understudy] Mission Control login: ${UNDERSTUDY_ADMIN_USERNAME:-admin} / (Secret Manager - show it: gcloud secrets versions access latest --secret=understudy-admin-password)"
else
  echo "[understudy] WARNING: UNDERSTUDY_ADMIN_PASSWORD is not set - Mission Control uses the PUBLISHED default. Redeploy with ./deploy_cloudrun.sh (it creates the secret)."
fi

cd /app
uvicorn server:app --host 127.0.0.1 --port 8000 --timeout-keep-alive 75 &
BACKEND=$!

# wait (max 60 s) until the backend answers, so the first request never hits a cold proxy
i=0
until curl -fs http://127.0.0.1:8000/api/health >/dev/null 2>&1; do
  i=$((i+1)); [ $i -ge 120 ] && { echo "[understudy] backend did not start"; exit 1; }
  kill -0 $BACKEND 2>/dev/null || { echo "[understudy] backend exited"; exit 1; }
  sleep 0.5
done
echo "[understudy] backend ready; web UI on :$PORT"

cd /app/understudy-ui
exec node node_modules/next/dist/bin/next start -p "$PORT" -H 0.0.0.0
