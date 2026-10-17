#!/usr/bin/env bash
# ==============================================================================
# Understudy - prepare Google Cloud Model Armor for Understudy (idempotent - safe to run again).
#
#   ./scripts/model_armor_setup.sh <project-id> [region] [member] [template-id]
#
#   member       who calls Model Armor at run time, e.g. serviceAccount:understudy-run@<project>.iam.gserviceaccount.com
#                (default: your gcloud account - what a local install uses through Application Default Credentials)
#   template-id  default: understudy-inputs
#
#   1. enables the Model Armor API (modelarmor.googleapis.com)
#   2. creates the template if it doesn't exist yet, with prompt-injection & jailbreak, malicious-URL and
#      responsible-AI filters at MEDIUM_AND_ABOVE (sensitive-data off: it would also block legitimate payloads)
#   3. grants <member> roles/modelarmor.user (screen texts); with UNDERSTUDY_MODEL_ARMOR_ADMIN=1 also
#      roles/modelarmor.admin, so Mission Control's "Create template in Google Cloud" button works
#   4. prints the template name. Turn screening on in Mission Control > AI, security & account > Google Cloud Model
#      Armor (or set UNDERSTUDY_MODEL_ARMOR_TEMPLATE=<name>; gcp_setup.sh / deploy_cloudrun.sh --model-armor do that).
# ==============================================================================
set -euo pipefail
PROJECT="${1:?usage: $0 <project-id> [region] [member] [template-id]}"
REGION="${2:-us-central1}"
MEMBER="${3:-user:$(gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -1)}"
TEMPLATE_ID="${4:-understudy-inputs}"
NAME="projects/${PROJECT}/locations/${REGION}/templates/${TEMPLATE_ID}"
API="https://modelarmor.${REGION}.rep.googleapis.com/v1"

echo "== Model Armor: enabling modelarmor.googleapis.com"
gcloud services enable modelarmor.googleapis.com --project "$PROJECT"

echo "== Model Armor: template ${NAME}"
TOKEN="$(gcloud auth print-access-token)"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" -H "x-goog-user-project: ${PROJECT}" \
  "${API}/${NAME}")
if [ "$CODE" = "200" ]; then
  echo "Template already exists"
else
  BODY='{"filterConfig":{"piAndJailbreakFilterSettings":{"filterEnforcement":"ENABLED","confidenceLevel":"MEDIUM_AND_ABOVE"},"maliciousUriFilterSettings":{"filterEnforcement":"ENABLED"},"raiSettings":{"raiFilters":[{"filterType":"HATE_SPEECH","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"HARASSMENT","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"DANGEROUS","confidenceLevel":"MEDIUM_AND_ABOVE"},{"filterType":"SEXUALLY_EXPLICIT","confidenceLevel":"MEDIUM_AND_ABOVE"}]}}}'
  OUT=$(curl -s -w '\n%{http_code}' -X POST -H "Authorization: Bearer ${TOKEN}" -H "x-goog-user-project: ${PROJECT}" \
    -H "Content-Type: application/json" "${API}/projects/${PROJECT}/locations/${REGION}/templates?templateId=${TEMPLATE_ID}" \
    -d "$BODY")
  if [ "$(echo "$OUT" | tail -1)" != "200" ]; then
    echo "Could not create the template:"; echo "$OUT" | head -20
    echo "Create it in the console (Security > Model Armor) or from Mission Control, then run this script again."
    exit 1
  fi
  echo "Template created"
fi

echo "== Model Armor: roles for ${MEMBER}"
ROLES="roles/modelarmor.user"
[ "${UNDERSTUDY_MODEL_ARMOR_ADMIN:-0}" = "1" ] && ROLES="$ROLES roles/modelarmor.admin"
for ROLE in $ROLES; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="$MEMBER" --role="$ROLE" --condition=None >/dev/null \
    && echo "granted ${ROLE}" || echo "could not grant ${ROLE} (project owners/editors may already have it)"
done
echo "MODEL_ARMOR_TEMPLATE=${NAME}"
