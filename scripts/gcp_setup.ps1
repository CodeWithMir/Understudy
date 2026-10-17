# ==============================================================================
# Understudy - connect THIS installation (native or Docker) to Google Cloud - Windows PowerShell.
#
#   powershell -ExecutionPolicy Bypass -File scripts\gcp_setup.ps1 <project-id> [region] [-NoFirestore] [-NoBucket] [-ModelArmor]
#   (or double-click / run scripts\gcp_setup.bat <project-id>)
#
# Same steps as scripts/gcp_setup.sh: gcloud login -> project + billing -> enable Vertex AI / Firestore / Cloud
# Storage APIs -> Application Default Credentials -> Firestore database + recordings bucket -> .env -> real check.
# -ModelArmor (7.2): also enable Google Cloud Model Armor, create the template 'understudy-inputs', grant you the
# Model Armor roles and set UNDERSTUDY_MODEL_ARMOR_TEMPLATE (or do it later in Mission Control > AI, security & account).
# ==============================================================================
param(
    [Parameter(Position = 0)][string]$Project,
    [Parameter(Position = 1)][string]$Region = "us-central1",
    [switch]$NoFirestore,
    [switch]$NoBucket,
    [switch]$ModelArmor
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $Project) {
    Write-Host "usage: scripts\gcp_setup.ps1 <project-id> [region] [-NoFirestore] [-NoBucket] [-ModelArmor]"
    Write-Host "       (list your projects with: gcloud projects list)"
    exit 2
}
$Bucket = if ($env:UNDERSTUDY_GCS_BUCKET) { $env:UNDERSTUDY_GCS_BUCKET } else { "$Project-understudy-recordings" }
$Py = if (Test-Path "venv\Scripts\python.exe") { "venv\Scripts\python.exe" } else { "python" }
function Step($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Gc { & gcloud @args; if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed" } }
function GcQuiet { & gcloud @args 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }

Step "[1/7] gcloud CLI and login"
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Host "The Google Cloud CLI (gcloud) is not installed. Install it:" -ForegroundColor Yellow
    Write-Host "  https://cloud.google.com/sdk/docs/install   (then open a NEW terminal)"
    exit 2
}
$account = (& gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>$null | Select-Object -First 1)
if (-not $account) { Gc auth login; $account = (& gcloud auth list --filter=status:ACTIVE --format="value(account)" | Select-Object -First 1) }
Write-Host "Logged in as $account"

Step "[2/7] project $Project"
Gc config set project $Project | Out-Null
if (-not (GcQuiet projects describe $Project)) {
    Write-Host "Project $Project not found or no access. Create one: https://console.cloud.google.com/projectcreate" -ForegroundColor Red
    exit 2
}
$billing = (& gcloud billing projects describe $Project --format="value(billingEnabled)" 2>$null)
if ("$billing" -match "^(False|false)$") {
    Write-Host "Billing is OFF for $Project. Turn it on: https://console.cloud.google.com/billing/linkedaccount?project=$Project" -ForegroundColor Red
    exit 2
}
Write-Host "Billing: $billing"

Step "[3/7] enabling APIs (Vertex AI, Firestore, Cloud Storage)"
$apis = @("aiplatform.googleapis.com")
if (-not $NoFirestore) { $apis += "firestore.googleapis.com" }
if (-not $NoBucket) { $apis += "storage.googleapis.com" }
Gc services enable @apis

Step "[4/7] Application Default Credentials (what Understudy uses - no API key)"
$adc = Join-Path $env:APPDATA "gcloud\application_default_credentials.json"
if ($env:GOOGLE_APPLICATION_CREDENTIALS) { Write-Host "Using the service-account key in GOOGLE_APPLICATION_CREDENTIALS" }
elseif (Test-Path $adc) { Write-Host "Found $adc" }
else { Gc auth application-default login }
GcQuiet auth application-default set-quota-project $Project | Out-Null

if (-not $NoFirestore) {
    Step "[5/7] Firestore database (Native mode, $Region)"
    if (GcQuiet firestore databases describe "--database=(default)") { Write-Host "Firestore database (default) already exists" }
    else { Gc firestore databases create "--location=$Region" --type=firestore-native }
}
if (-not $NoBucket) {
    Step "[5/7] recordings bucket gs://$Bucket"
    if (GcQuiet storage buckets describe "gs://$Bucket") { Write-Host "Bucket already exists" }
    else {
        Gc storage buckets create "gs://$Bucket" "--location=$Region" --uniform-bucket-level-access
        $lc = Join-Path $env:TEMP "understudy-lifecycle.json"
        '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7}}]}' | Set-Content -Encoding ascii $lc
        Gc storage buckets update "gs://$Bucket" "--lifecycle-file=$lc"
    }
}
foreach ($role in @("roles/aiplatform.user", "roles/datastore.user", "roles/storage.objectAdmin")) {
    GcQuiet projects add-iam-policy-binding $Project "--member=user:$account" "--role=$role" --condition=None | Out-Null
}

$armorTpl = ""
if ($ModelArmor) {
    Step "[5/7] Google Cloud Model Armor"
    $tplId = "understudy-inputs"
    $armorTpl = "projects/$Project/locations/$Region/templates/$tplId"
    Gc services enable modelarmor.googleapis.com
    $token = (& gcloud auth print-access-token).Trim()
    $hdr = @{ Authorization = "Bearer $token"; "x-goog-user-project" = $Project }
    $api = "https://modelarmor.$Region.rep.googleapis.com/v1"
    $exists = $true
    try { Invoke-RestMethod -Headers $hdr -Uri "$api/$armorTpl" | Out-Null } catch { $exists = $false }
    if ($exists) { Write-Host "Template already exists" }
    else {
        $rai = @("HATE_SPEECH", "HARASSMENT", "DANGEROUS", "SEXUALLY_EXPLICIT") | ForEach-Object { @{ filterType = $_; confidenceLevel = "MEDIUM_AND_ABOVE" } }
        $body = @{ filterConfig = @{
            piAndJailbreakFilterSettings = @{ filterEnforcement = "ENABLED"; confidenceLevel = "MEDIUM_AND_ABOVE" }
            maliciousUriFilterSettings = @{ filterEnforcement = "ENABLED" }
            raiSettings = @{ raiFilters = @($rai) } } } | ConvertTo-Json -Depth 6
        Invoke-RestMethod -Method Post -Headers $hdr -ContentType "application/json" -Body $body `
            -Uri "$api/projects/$Project/locations/$Region/templates?templateId=$tplId" | Out-Null
        Write-Host "Template created: $armorTpl"
    }
    foreach ($role in @("roles/modelarmor.user", "roles/modelarmor.admin")) {
        GcQuiet projects add-iam-policy-binding $Project "--member=user:$account" "--role=$role" --condition=None | Out-Null
    }
}

Step "[6/7] writing .env and installing the Google Cloud clients"
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
$store = if ($NoFirestore) { "local" } else { "firestore" }
$gcs = if ($NoBucket) { "" } else { $Bucket }
$code = "import sys; sys.path.insert(0,'.'); from core.wizard import env_file; " +
        "env_file.update({'GOOGLE_GENAI_USE_VERTEXAI':'true','GOOGLE_CLOUD_PROJECT':sys.argv[1],'GOOGLE_CLOUD_LOCATION':sys.argv[2]," +
        "'UNDERSTUDY_STORE':sys.argv[3],'UNDERSTUDY_GCS_BUCKET':sys.argv[4]," +
        "**({'UNDERSTUDY_MODEL_ARMOR_TEMPLATE':sys.argv[5]} if len(sys.argv)>5 and sys.argv[5] else {})}); print('.env updated')"
& $Py -c $code $Project $Region $store $gcs $armorTpl
& $Py -m pip install -q -r requirements-gcp.txt

Step "[7/7] checking everything for real (Gemini call, Firestore, Cloud Storage)"
& $Py scripts\gcp_check.py
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Fix the items marked FAIL (each shows the command), then run: $Py scripts\gcp_check.py" -ForegroundColor Yellow
    exit 1
}
Write-Host ""
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "  Google Cloud is connected. Start Understudy:  run_demo.bat   (or deploy_docker.bat)"
Write-Host "  Mission Control > AI, security & account shows: Vertex AI - Cloud Firestore"
Write-Host "  Deploy to Cloud Run (from Cloud Shell or Git Bash/WSL):  ./deploy_cloudrun.sh $Project $Region"
Write-Host "================================================================================" -ForegroundColor Green
