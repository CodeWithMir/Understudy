#!/usr/bin/env bash
# ==============================================================================
# Understudy - deploy to Google Cloud Run (run it from Cloud Shell or any machine with gcloud).
#
#   ./deploy_cloudrun.sh <project-id> [region] [--model-armor]
#
# One-time setup it performs (idempotent - safe to run again for every new version):
#   * enables Cloud Run, Cloud Build, Artifact Registry, Vertex AI, Firestore and Cloud Storage APIs
#   * creates the Firestore database (Native mode), an Artifact Registry repo and a recordings bucket
#     (objects auto-deleted after 7 days)
#   * creates the service account "understudy-run" with ONLY: Vertex AI User, Cloud Datastore User (Firestore),
#     Storage Object Admin on the recordings bucket
#   * --model-armor (7.2): enables Google Cloud Model Armor, creates the template 'understudy-inputs', gives the
#     service account roles/modelarmor.user (UNDERSTUDY_MODEL_ARMOR_ADMIN=1: also roles/modelarmor.admin, for
#     Mission Control's 'Create template' button) and sets UNDERSTUDY_MODEL_ARMOR_TEMPLATE. Without the flag, an admin
#     can still turn it on later in Mission Control > AI, security & account > Google Cloud Model Armor.
# Then builds Dockerfile.cloudrun with Cloud Build and deploys it. No API key is used: Gemini runs on Vertex AI with
# the service account. 9.7: the Mission Control password is NOT the published default on a public URL - the first
# deployment generates one, keeps it in Secret Manager (secret "understudy-admin-password") and prints it once; read it
# again with: gcloud secrets versions access latest --secret=understudy-admin-password
# At the end the script checks the live URL against the submission requirements (scripts/check_deployment.py).
#
# Optional environment variables:
#   UNDERSTUDY_SIGNUP=on                          allow self-signup from the start (default off on Cloud Run)
#   UNDERSTUDY_ADMIN_PASSWORD_SECRET=<secret-name>  your own Secret Manager secret for the admin password
#                                                   (default: understudy-admin-password, generated on first deploy;
#                                                   "none" keeps the README default - not recommended on a public URL)
#   UNDERSTUDY_MAX_INSTANCES=3   UNDERSTUDY_SERVICE=understudy   UNDERSTUDY_GCS_BUCKET=<bucket>
# ==============================================================================
set -euo pipefail
ARMOR=0; POS=()
for a in "$@"; do
  case "$a" in
    --model-armor) ARMOR=1 ;;
    *) POS+=("$a") ;;
  esac
done
PROJECT="${POS[0]:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${POS[1]:-us-central1}"
SERVICE="${UNDERSTUDY_SERVICE:-understudy}"
REPO=understudy
BUCKET="${UNDERSTUDY_GCS_BUCKET:-${PROJECT}-understudy-recordings}"
SA_NAME=understudy-run
MAX_INSTANCES="${UNDERSTUDY_MAX_INSTANCES:-3}"

if [ -z "$PROJECT" ]; then echo "usage: $0 <project-id> [region] [--model-armor]"; exit 2; fi
command -v gcloud >/dev/null || { echo "gcloud CLI not found - use Cloud Shell or install the Google Cloud SDK"; exit 2; }
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
VERSION="$(sed -n 's/^WIZARD_VERSION = "\([^"]*\)".*/\1/p' "$(dirname "$0")/core/wizard/__init__.py")"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/understudy:${VERSION}-$(date +%Y%m%d%H%M%S)"
gcloud config set project "$PROJECT" >/dev/null

echo "== [1/5] enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com storage.googleapis.com iam.googleapis.com

echo "== [2/5] Firestore, Artifact Registry, recordings bucket"
gcloud firestore databases describe --database="(default)" >/dev/null 2>&1 || \
  gcloud firestore databases create --location="$REGION" --type=firestore-native
gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --location="$REGION" --repository-format=docker \
    --description="Understudy images"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access
  printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}' > /tmp/understudy-lifecycle.json
  gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/understudy-lifecycle.json
fi

echo "== [3/5] least-privilege service account ${SA}"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="Understudy on Cloud Run"
for ROLE in roles/aiplatform.user roles/datastore.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${SA}" --role="$ROLE" \
    --condition=None >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="serviceAccount:${SA}" \
  --role=roles/storage.objectAdmin >/dev/null
ARMOR_ENV=""
if [ $ARMOR = 1 ]; then
  ./scripts/model_armor_setup.sh "$PROJECT" "$REGION" "serviceAccount:${SA}" | tee /tmp/understudy-armor.log
  ARMOR_ENV=",UNDERSTUDY_MODEL_ARMOR_TEMPLATE=$(sed -n 's/^MODEL_ARMOR_TEMPLATE=//p' /tmp/understudy-armor.log | tail -1)"
fi

echo "== [4/5] building ${IMAGE} with Cloud Build (Dockerfile.cloudrun)"
BUILD_CFG="$(mktemp)"
cat > "$BUILD_CFG" <<YAML
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-f", "Dockerfile.cloudrun", "-t", "${IMAGE}", "."]
images: ["${IMAGE}"]
timeout: 3600s
YAML
gcloud builds submit --config="$BUILD_CFG" --timeout=3600s .
rm -f "$BUILD_CFG"

SECRET_ARGS=""
ADMIN_SECRET="${UNDERSTUDY_ADMIN_PASSWORD_SECRET:-understudy-admin-password}"
NEW_ADMIN_PASSWORD=""
if [ "$ADMIN_SECRET" != "none" ]; then
  # The admin password lives in Secret Manager (survives redeployments; never in the image or the env vars). On the
  # first deployment it is generated here and printed once at the end.
  gcloud services enable secretmanager.googleapis.com >/dev/null
  if ! gcloud secrets describe "$ADMIN_SECRET" >/dev/null 2>&1; then
    NEW_ADMIN_PASSWORD="$(python3 -c 'import secrets,string; a=string.ascii_letters+string.digits; print("Us-" + "".join(secrets.choice(a) for _ in range(18)) + "-9")')"
    printf '%s' "$NEW_ADMIN_PASSWORD" | gcloud secrets create "$ADMIN_SECRET" --replication-policy=automatic --data-file=- >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$ADMIN_SECRET" --member="serviceAccount:${SA}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  SECRET_ARGS="--update-secrets=UNDERSTUDY_ADMIN_PASSWORD=${ADMIN_SECRET}:latest"
  echo "Admin password: Secret Manager secret ${ADMIN_SECRET}"
else
  echo "WARNING: the admin password is the published README default - anyone with the URL can open Mission Control."
fi

# Self-signup is OFF on Cloud Run unless you opt in (the service is reachable from the internet): only accounts the
# admin creates can log in. Opt in with UNDERSTUDY_SIGNUP=on ./deploy_cloudrun.sh ..., or later in Mission Control >
# Users > "Allow self-signup" (the admin switch always wins over this default).
SIGNUP_ENV=",UNDERSTUDY_SIGNUP=${UNDERSTUDY_SIGNUP:-off}"
echo "Self-signup default: ${UNDERSTUDY_SIGNUP:-off} (change it any time in Mission Control > Users)"

# Email (README 2.0.2): set UNDERSTUDY_SMTP_HOST / _PORT / _USER / UNDERSTUDY_MAIL_FROM in the shell before deploying,
# and put the SMTP password in Secret Manager: UNDERSTUDY_SMTP_PASSWORD_SECRET=<secret-name>.
MAIL_ENV=""
if [ -n "${UNDERSTUDY_SMTP_HOST:-}" ]; then
  MAIL_ENV=",UNDERSTUDY_SMTP_HOST=${UNDERSTUDY_SMTP_HOST},UNDERSTUDY_SMTP_PORT=${UNDERSTUDY_SMTP_PORT:-587},UNDERSTUDY_SMTP_SECURITY=${UNDERSTUDY_SMTP_SECURITY:-starttls},UNDERSTUDY_SMTP_USER=${UNDERSTUDY_SMTP_USER:-},UNDERSTUDY_MAIL_FROM=${UNDERSTUDY_MAIL_FROM:-${UNDERSTUDY_SMTP_USER:-}},UNDERSTUDY_ADMIN_CONTACT=${UNDERSTUDY_ADMIN_CONTACT:-}"
  echo "Email: SMTP ${UNDERSTUDY_SMTP_HOST}"
else
  echo "Email: not configured (outbox mode - signup, password reset and two-step sign-in codes can't be delivered). See README 2.0.2."
fi
if [ -n "${UNDERSTUDY_SMTP_PASSWORD_SECRET:-}" ]; then
  gcloud services enable secretmanager.googleapis.com >/dev/null
  gcloud secrets add-iam-policy-binding "$UNDERSTUDY_SMTP_PASSWORD_SECRET" --member="serviceAccount:${SA}" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  SECRET_ARGS="${SECRET_ARGS:+${SECRET_ARGS},}"
  SECRET_ARGS="${SECRET_ARGS:---update-secrets=}UNDERSTUDY_SMTP_PASSWORD=${UNDERSTUDY_SMTP_PASSWORD_SECRET}:latest"
fi

echo "== [5/5] deploying Cloud Run service ${SERVICE} (${REGION})"
gcloud run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --service-account="$SA" \
  --allow-unauthenticated --port=8080 --no-cpu-throttling --cpu=2 --memory=4Gi --timeout=3600 \
  --concurrency=20 --min-instances=1 --max-instances="$MAX_INSTANCES" --session-affinity \
  --execution-environment=gen2 \
  --update-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${REGION},UNDERSTUDY_STORE=firestore,UNDERSTUDY_GCS_BUCKET=${BUCKET}${ARMOR_ENV}${MAIL_ENV}${SIGNUP_ENV}" \
  --startup-probe=httpGet.path=/healthz,periodSeconds=5,failureThreshold=24 \
  --liveness-probe=httpGet.path=/healthz,periodSeconds=30,failureThreshold=3 \
  ${SECRET_ARGS}

URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')
echo
echo "================================================================================"
echo "  Understudy v${VERSION} is live on Google Cloud Run (Gemini on Vertex AI, Firestore)"
echo "  Studio:  ${URL}/wizard        Mission Control:  ${URL}/"
if [ -n "$NEW_ADMIN_PASSWORD" ]; then
  echo "  Mission Control login: admin / ${NEW_ADMIN_PASSWORD}   <- shown ONCE; it is in Secret Manager (${ADMIN_SECRET})"
elif [ "$ADMIN_SECRET" != "none" ]; then
  echo "  Mission Control login: admin / (gcloud secrets versions access latest --secret=${ADMIN_SECRET})"
else
  echo "  Mission Control login: admin / Understudy@2026  - CHANGE IT NOW (public URL)"
fi
echo "  Then: Mission Control > Users > create the Studio accounts; Access & tools > approve tools"
echo "  Check the Google Cloud set-up: Mission Control > AI, security & account > Check Google Cloud set-up"
echo "  Demo ERP: ${URL}/portal1  ${URL}/portal2  ${URL}/portal3      Health: ${URL}/healthz"
echo "================================================================================"
echo
echo "== submission check (scripts/check_deployment.py)"
python3 "$(dirname "$0")/scripts/check_deployment.py" "$URL" || true
