#!/usr/bin/env bash
# ==============================================================================
# Understudy - connect THIS installation (native or Docker) to Google Cloud.
#
#   ./scripts/gcp_setup.sh <project-id> [region] [--no-firestore] [--no-bucket] [--model-armor]
#
# What it does (idempotent - safe to run again):
#   1. checks the gcloud CLI and that you are logged in (opens the browser if not)
#   2. selects the project and checks that billing is on
#   3. enables the Vertex AI, Firestore and Cloud Storage APIs
#   4. creates Application Default Credentials for Understudy (gcloud auth application-default login)
#   5. creates the Firestore database (Native mode) and a recordings bucket (objects deleted after 7 days)
#   6. writes the settings into .env and installs the Google Cloud Python clients
#   7. runs scripts/gcp_check.py - a real Gemini call, Firestore and Cloud Storage round-trips
#   --model-armor (7.2): also enables Google Cloud Model Armor, creates the template 'understudy-inputs', grants you
#      the Model Armor roles and sets UNDERSTUDY_MODEL_ARMOR_TEMPLATE (scripts/model_armor_setup.sh). You can also do
#      all of this later in Mission Control > AI, security & account > Google Cloud Model Armor.
# Then start Understudy as usual (./run_demo.sh or ./deploy_docker.sh). To deploy to Cloud Run use ./deploy_cloudrun.sh.
# ==============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=""; REGION="us-central1"; FIRESTORE=1; BUCKET_ON=1; ARMOR=0
for a in "$@"; do
  case "$a" in
    --no-firestore) FIRESTORE=0 ;;
    --no-bucket) BUCKET_ON=0 ;;
    --model-armor) ARMOR=1 ;;
    -h|--help) sed -n 2,21p "$0"; exit 0 ;;
    *) if [ -z "$PROJECT" ]; then PROJECT="$a"; else REGION="$a"; fi ;;
  esac
done
if [ -z "$PROJECT" ]; then
  echo "usage: $0 <project-id> [region] [--no-firestore] [--no-bucket] [--model-armor]"
  echo "       (list your projects with: gcloud projects list)"
  exit 2
fi
BUCKET="${UNDERSTUDY_GCS_BUCKET:-${PROJECT}-understudy-recordings}"
PY=python3; [ -x venv/bin/python ] && PY=venv/bin/python

step() { echo; echo "== $*"; }

step "[1/7] gcloud CLI and login"
if ! command -v gcloud >/dev/null 2>&1; then
  echo "The Google Cloud CLI (gcloud) is not installed."
  echo "Install it: https://cloud.google.com/sdk/docs/install   (or run this script in Cloud Shell)"
  exit 2
fi
if [ -z "$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)" ]; then
  gcloud auth login
fi
ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -1)"
echo "Logged in as ${ACCOUNT}"

step "[2/7] project ${PROJECT}"
gcloud config set project "$PROJECT" >/dev/null
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  echo "Project ${PROJECT} not found or no access. Create one: https://console.cloud.google.com/projectcreate"
  exit 2
fi
BILLING="$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null || echo unknown)"
if [ "$BILLING" = "False" ] || [ "$BILLING" = "false" ]; then
  echo "Billing is OFF for ${PROJECT}. Vertex AI, Firestore and Cloud Storage need it:"
  echo "  https://console.cloud.google.com/billing/linkedaccount?project=${PROJECT}"
  exit 2
fi
echo "Billing: ${BILLING}"

step "[3/7] enabling APIs (Vertex AI, Firestore, Cloud Storage)"
APIS="aiplatform.googleapis.com"
[ $FIRESTORE = 1 ] && APIS="$APIS firestore.googleapis.com"
[ $BUCKET_ON = 1 ] && APIS="$APIS storage.googleapis.com"
gcloud services enable $APIS

step "[4/7] Application Default Credentials (what Understudy uses - no API key)"
ADC="${CLOUDSDK_CONFIG:-$HOME/.config/gcloud}/application_default_credentials.json"
if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "Using the service-account key in GOOGLE_APPLICATION_CREDENTIALS=${GOOGLE_APPLICATION_CREDENTIALS}"
elif [ -f "$ADC" ]; then
  echo "Found ${ADC}"
else
  gcloud auth application-default login
fi
gcloud auth application-default set-quota-project "$PROJECT" >/dev/null 2>&1 || true

if [ $FIRESTORE = 1 ]; then
  step "[5/7] Firestore database (Native mode, ${REGION})"
  if gcloud firestore databases describe --database="(default)" >/dev/null 2>&1; then
    echo "Firestore database (default) already exists"
  else
    gcloud firestore databases create --location="$REGION" --type=firestore-native
  fi
fi
if [ $BUCKET_ON = 1 ]; then
  step "[5/7] recordings bucket gs://${BUCKET}"
  if gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
    echo "Bucket already exists"
  else
    gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access
    printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}' > /tmp/understudy-lifecycle.json
    gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/understudy-lifecycle.json
  fi
fi
# Project owners/editors already have these roles; grant them explicitly for other accounts (ignored if not allowed).
for ROLE in roles/aiplatform.user roles/datastore.user roles/storage.objectAdmin; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="user:${ACCOUNT}" --role="$ROLE" --condition=None \
    >/dev/null 2>&1 || true
done

ARMOR_TPL=""
if [ $ARMOR = 1 ]; then
  step "[5/7] Google Cloud Model Armor"
  UNDERSTUDY_MODEL_ARMOR_ADMIN=1 ./scripts/model_armor_setup.sh "$PROJECT" "$REGION" "user:${ACCOUNT}" | tee /tmp/understudy-armor.log
  ARMOR_TPL="$(sed -n 's/^MODEL_ARMOR_TEMPLATE=//p' /tmp/understudy-armor.log | tail -1)"
fi

step "[6/7] writing .env and installing the Google Cloud clients"
[ -f .env ] || cp .env.example .env
STORE=local; [ $FIRESTORE = 1 ] && STORE=firestore
GCS=""; [ $BUCKET_ON = 1 ] && GCS="$BUCKET"
"$PY" - "$PROJECT" "$REGION" "$STORE" "$GCS" "$ARMOR_TPL" <<'PYEOF'
import sys
sys.path.insert(0, ".")
from core.wizard import env_file
project, region, store, bucket, armor = sys.argv[1:6]
values = {"GOOGLE_GENAI_USE_VERTEXAI": "true", "GOOGLE_CLOUD_PROJECT": project,
          "GOOGLE_CLOUD_LOCATION": region, "UNDERSTUDY_STORE": store, "UNDERSTUDY_GCS_BUCKET": bucket}
if armor:
    values["UNDERSTUDY_MODEL_ARMOR_TEMPLATE"] = armor
    print(".env: UNDERSTUDY_MODEL_ARMOR_TEMPLATE=%s (Mission Control can override it)" % armor)
env_file.update(values)
print(".env updated: GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_PROJECT=%s, GOOGLE_CLOUD_LOCATION=%s, "
      "UNDERSTUDY_STORE=%s, UNDERSTUDY_GCS_BUCKET=%s" % (project, region, store, bucket or "(none)"))
PYEOF
"$PY" -m pip install -q -r requirements-gcp.txt

step "[7/7] checking everything for real (Gemini call, Firestore, Cloud Storage)"
"$PY" scripts/gcp_check.py || { echo; echo "Fix the items marked FAIL (each shows the command), then run: $PY scripts/gcp_check.py"; exit 1; }

echo
echo "================================================================================"
echo "  Google Cloud is connected. Start Understudy:  ./run_demo.sh   (or ./deploy_docker.sh)"
echo "  Mission Control > AI, security & account shows: Vertex AI · Cloud Firestore"
echo "  Deploy to Cloud Run instead:                  ./deploy_cloudrun.sh ${PROJECT} ${REGION}"
echo "================================================================================"
