# ==============================================================================
# Understudy - deploy to Google Cloud Run from Windows (PowerShell 5.1+ with the Google Cloud CLI "gcloud").
#
#   .\deploy_cloudrun.ps1 <project-id> [region]          (or: deploy_cloudrun.bat <project-id> [region])
#
# Same steps as deploy_cloudrun.sh (Cloud Shell / macOS / Linux), idempotent - run it again for every new version:
#   1. enables Cloud Run, Cloud Build, Artifact Registry, Vertex AI, Firestore, Cloud Storage, Secret Manager APIs
#   2. creates the Firestore database (Native mode), an Artifact Registry repo and a recordings bucket (7-day expiry)
#   3. creates the service account "understudy-run" with ONLY: Vertex AI User, Cloud Datastore User (Firestore),
#      Storage Object Admin on the recordings bucket, Secret Accessor on the admin-password secret
#   4. builds Dockerfile.cloudrun with Cloud Build
#   5. deploys the Cloud Run service (Gemini on Vertex AI through the service account - no API key)
# The Mission Control password is generated on the first deployment, kept in Secret Manager
# ("understudy-admin-password") and printed once. Google Cloud Model Armor (--model-armor in the bash script) can be
# switched on later in Mission Control > AI, security & account.
#
# Optional environment variables (same as the bash script):
#   UNDERSTUDY_SIGNUP=on   UNDERSTUDY_MAX_INSTANCES=3   UNDERSTUDY_SERVICE=understudy   UNDERSTUDY_GCS_BUCKET=<bucket>
#   UNDERSTUDY_ADMIN_PASSWORD_SECRET=<secret-name>|none
#   UNDERSTUDY_SMTP_HOST / _PORT / _SECURITY / _USER, UNDERSTUDY_MAIL_FROM, UNDERSTUDY_ADMIN_CONTACT,
#   UNDERSTUDY_SMTP_PASSWORD_SECRET=<secret-name>
# ==============================================================================
param(
    [Parameter(Position = 0)][string]$Project = $env:GOOGLE_CLOUD_PROJECT,
    [Parameter(Position = 1)][string]$Region = "us-central1"
)
$ErrorActionPreference = "Continue"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT

function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Invoke-Gcloud {            # run gcloud, stop on failure
    & gcloud @args
    if ($LASTEXITCODE -ne 0) { Fail "gcloud $($args -join ' ') failed (exit $LASTEXITCODE)" }
}
function Test-Gcloud {          # run gcloud quietly, return $true when it succeeded
    & gcloud @args *> $null
    return ($LASTEXITCODE -eq 0)
}
function Env-Or([string]$name, [string]$default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrEmpty($v)) { return $default } else { return $v }
}
function Write-NoNewline([string]$path, [string]$text) {   # secrets and JSON without a trailing newline
    [IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

if ([string]::IsNullOrEmpty($Project)) { Write-Host "usage: .\deploy_cloudrun.ps1 <project-id> [region]"; exit 2 }
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Fail "gcloud CLI not found - install the Google Cloud SDK (https://cloud.google.com/sdk/docs/install) or use Cloud Shell"
}

$Service = Env-Or "UNDERSTUDY_SERVICE" "understudy"
$Repo = "understudy"
$Bucket = Env-Or "UNDERSTUDY_GCS_BUCKET" "$Project-understudy-recordings"
$SaName = "understudy-run"
$Sa = "$SaName@$Project.iam.gserviceaccount.com"
$MaxInstances = Env-Or "UNDERSTUDY_MAX_INSTANCES" "3"
$AdminSecret = Env-Or "UNDERSTUDY_ADMIN_PASSWORD_SECRET" "understudy-admin-password"
$Version = (Select-String -Path (Join-Path $ROOT (Join-Path "core" (Join-Path "wizard" "__init__.py"))) -Pattern '^WIZARD_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
$Image = "$Region-docker.pkg.dev/$Project/$Repo/understudy:$Version-$(Get-Date -Format yyyyMMddHHmmss)"
$Tmp = [IO.Path]::GetTempPath()

Invoke-Gcloud config set project $Project

Write-Host "== [1/5] enabling APIs" -ForegroundColor Cyan
Invoke-Gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    aiplatform.googleapis.com firestore.googleapis.com storage.googleapis.com iam.googleapis.com secretmanager.googleapis.com

Write-Host "== [2/5] Firestore, Artifact Registry, recordings bucket" -ForegroundColor Cyan
if (-not (Test-Gcloud firestore databases describe "--database=(default)")) {
    Invoke-Gcloud firestore databases create "--location=$Region" --type=firestore-native
}
if (-not (Test-Gcloud artifacts repositories describe $Repo "--location=$Region")) {
    Invoke-Gcloud artifacts repositories create $Repo "--location=$Region" --repository-format=docker "--description=Understudy images"
}
if (-not (Test-Gcloud storage buckets describe "gs://$Bucket")) {
    Invoke-Gcloud storage buckets create "gs://$Bucket" "--location=$Region" --uniform-bucket-level-access
    $life = Join-Path $Tmp "understudy-lifecycle.json"
    Write-NoNewline $life '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}'
    Invoke-Gcloud storage buckets update "gs://$Bucket" "--lifecycle-file=$life"
    Remove-Item $life -ErrorAction SilentlyContinue
}

Write-Host "== [3/5] least-privilege service account $Sa" -ForegroundColor Cyan
if (-not (Test-Gcloud iam service-accounts describe $Sa)) {
    Invoke-Gcloud iam service-accounts create $SaName "--display-name=Understudy on Cloud Run"
}
foreach ($role in @("roles/aiplatform.user", "roles/datastore.user")) {
    Invoke-Gcloud projects add-iam-policy-binding $Project "--member=serviceAccount:$Sa" "--role=$role" --condition=None --quiet | Out-Null
}
Invoke-Gcloud storage buckets add-iam-policy-binding "gs://$Bucket" "--member=serviceAccount:$Sa" --role=roles/storage.objectAdmin | Out-Null

$SecretArgs = @()
$NewAdminPassword = ""
if ($AdminSecret -ne "none") {
    if (-not (Test-Gcloud secrets describe $AdminSecret)) {
        $chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        $bytes = New-Object byte[] 18
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $NewAdminPassword = "Us-" + (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })) + "-9"
        $pwFile = Join-Path $Tmp "understudy-admin-pw.txt"
        Write-NoNewline $pwFile $NewAdminPassword
        Invoke-Gcloud secrets create $AdminSecret --replication-policy=automatic "--data-file=$pwFile" | Out-Null
        Remove-Item $pwFile -ErrorAction SilentlyContinue
    }
    Invoke-Gcloud secrets add-iam-policy-binding $AdminSecret "--member=serviceAccount:$Sa" --role=roles/secretmanager.secretAccessor | Out-Null
    $SecretArgs += "UNDERSTUDY_ADMIN_PASSWORD=${AdminSecret}:latest"
    Write-Host "Admin password: Secret Manager secret $AdminSecret"
} else {
    Write-Host "WARNING: the admin password is the published README default - anyone with the URL can open Mission Control." -ForegroundColor Yellow
}

Write-Host "== [4/5] building $Image with Cloud Build (Dockerfile.cloudrun, ~10 min the first time)" -ForegroundColor Cyan
$cfg = Join-Path $Tmp "understudy-cloudbuild.yaml"
Write-NoNewline $cfg @"
steps:
- name: gcr.io/cloud-builders/docker
  args: ["build", "-f", "Dockerfile.cloudrun", "-t", "$Image", "."]
images: ["$Image"]
timeout: 3600s
"@
Invoke-Gcloud builds submit "--config=$cfg" --timeout=3600s .
Remove-Item $cfg -ErrorAction SilentlyContinue

$Signup = Env-Or "UNDERSTUDY_SIGNUP" "off"
$EnvVars = "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=$Project,GOOGLE_CLOUD_LOCATION=$Region,UNDERSTUDY_STORE=firestore,UNDERSTUDY_GCS_BUCKET=$Bucket,UNDERSTUDY_SIGNUP=$Signup"
$SmtpHost = Env-Or "UNDERSTUDY_SMTP_HOST" ""
if ($SmtpHost) {
    $SmtpUser = Env-Or "UNDERSTUDY_SMTP_USER" ""
    $EnvVars += ",UNDERSTUDY_SMTP_HOST=$SmtpHost,UNDERSTUDY_SMTP_PORT=$(Env-Or 'UNDERSTUDY_SMTP_PORT' '587'),UNDERSTUDY_SMTP_SECURITY=$(Env-Or 'UNDERSTUDY_SMTP_SECURITY' 'starttls'),UNDERSTUDY_SMTP_USER=$SmtpUser,UNDERSTUDY_MAIL_FROM=$(Env-Or 'UNDERSTUDY_MAIL_FROM' $SmtpUser),UNDERSTUDY_ADMIN_CONTACT=$(Env-Or 'UNDERSTUDY_ADMIN_CONTACT' '')"
    Write-Host "Email: SMTP $SmtpHost"
} else {
    Write-Host "Email: not configured (outbox mode - signup, password reset and two-step sign-in codes can't be delivered). See README 2.0.2."
}
$SmtpSecret = Env-Or "UNDERSTUDY_SMTP_PASSWORD_SECRET" ""
if ($SmtpSecret) {
    Invoke-Gcloud secrets add-iam-policy-binding $SmtpSecret "--member=serviceAccount:$Sa" --role=roles/secretmanager.secretAccessor | Out-Null
    $SecretArgs += "UNDERSTUDY_SMTP_PASSWORD=${SmtpSecret}:latest"
}

Write-Host "== [5/5] deploying Cloud Run service $Service ($Region)" -ForegroundColor Cyan
$deploy = @("run", "deploy", $Service, "--image=$Image", "--region=$Region", "--service-account=$Sa",
    "--allow-unauthenticated", "--port=8080", "--no-cpu-throttling", "--cpu=2", "--memory=4Gi", "--timeout=3600",
    "--concurrency=20", "--min-instances=1", "--max-instances=$MaxInstances", "--session-affinity",
    "--execution-environment=gen2", "--update-env-vars=$EnvVars",
    "--startup-probe=httpGet.path=/healthz,periodSeconds=5,failureThreshold=24",
    "--liveness-probe=httpGet.path=/healthz,periodSeconds=30,failureThreshold=3")
if ($SecretArgs.Count -gt 0) { $deploy += "--update-secrets=$($SecretArgs -join ',')" }
Invoke-Gcloud @deploy

$Url = (& gcloud run services describe $Service "--region=$Region" "--format=value(status.url)").Trim()
Write-Host ""
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "  Understudy v$Version is live on Google Cloud Run (Gemini on Vertex AI, Firestore)" -ForegroundColor Green
Write-Host "  Studio:  $Url/wizard        Mission Control:  $Url/" -ForegroundColor Yellow
if ($NewAdminPassword) {
    Write-Host "  Mission Control login: admin / $NewAdminPassword   <- shown ONCE; it is in Secret Manager ($AdminSecret)" -ForegroundColor Yellow
} elseif ($AdminSecret -ne "none") {
    Write-Host "  Mission Control login: admin / (gcloud secrets versions access latest --secret=$AdminSecret)" -ForegroundColor Yellow
} else {
    Write-Host "  Mission Control login: admin / Understudy@2026  - CHANGE IT NOW (public URL)" -ForegroundColor Red
}
Write-Host "  Then: Mission Control > Users > create the Studio accounts; Access & tools > approve tools"
Write-Host "  Demo ERP: $Url/portal1  $Url/portal2  $Url/portal3      Health: $Url/healthz"
Write-Host "================================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "== submission check (scripts\check_deployment.py)" -ForegroundColor Cyan
$py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { $null }
if ($py) { & $py (Join-Path (Join-Path $ROOT "scripts") "check_deployment.py") $Url } else { Write-Host "Python not found - run: python scripts\check_deployment.py $Url" }
