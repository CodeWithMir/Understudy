# Understudy - Windows launcher (PowerShell).
# Runs the first-time setup if needed (setup.bat), then starts the backend (port 8000) and the web UI (port 3000).
#   powershell -ExecutionPolicy Bypass -File run_demo.ps1
# Ctrl+C stops both.
$ErrorActionPreference = "Continue"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$VERSION = (Select-String -Path "$ROOT\core\wizard\__init__.py" -Pattern '^WIZARD_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
$Host.UI.RawUI.WindowTitle = "Understudy v$VERSION"

Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "  UNDERSTUDY v$VERSION  //  Watch -> Build -> Govern -> Execute -> Export" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Cyan

# Gemini key: optional here - admins can also set it in Mission Control > AI, security & account.
$apiKey = $env:GEMINI_API_KEY
if (-not $apiKey -and (Test-Path "$ROOT\.env")) {
    foreach ($line in (Get-Content "$ROOT\.env" -ErrorAction SilentlyContinue)) {
        if ($line -match "^(GEMINI_API_KEY|GOOGLE_API_KEY)=(.+)$") { $apiKey = $matches[2].Trim("'`" ") }
    }
}
if ($apiKey) {
    $masked = $apiKey.Substring(0, [Math]::Min(4, $apiKey.Length)) + "..." + $apiKey.Substring([Math]::Max(0, $apiKey.Length - 4))
    Write-Host "  Gemini AI:  key found ($masked)" -ForegroundColor Green
} else {
    Write-Host "  Gemini AI:  no key in .env - add one in Mission Control > AI, security & account, or use Vertex AI (offline heuristics until then)" -ForegroundColor Yellow
}

# First-run setup (idempotent: venv, pip deps, Playwright Chromium, npm ci, web UI build)
cmd.exe /c "`"$ROOT\setup.bat`""
if ($LASTEXITCODE -ne 0) { Write-Host "Setup failed - see the messages above." -ForegroundColor Red; exit 1 }

Write-Host "`n[CHECK] Toolchains the automation tools need on THIS machine:" -ForegroundColor Magenta
foreach ($t in @(@("node", "Cypress, Playwright JS, Puppeteer, WebdriverIO"), @("npx", "Cypress, Playwright JS, Puppeteer, WebdriverIO"),
                 @("java", "REST Assured, Selenium Java - Understudy can download a JDK: Mission Control > Environments"),
                 @("mvn", "Maven builds - Understudy can download Maven: Mission Control > Access & tools"))) {
    if (Get-Command $t[0] -ErrorAction SilentlyContinue) { Write-Host ("   {0,-8} found   ({1})" -f $t[0], $t[1]) -ForegroundColor Gray }
    else { Write-Host ("   {0,-8} MISSING ({1})" -f $t[0], $t[1]) -ForegroundColor Yellow }
}
$browsers = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
              "$env:LocalAppData\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
              "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { $_ -and (Test-Path $_) }
if ($browsers) { Write-Host "   browser  found   ($($browsers[0]))" -ForegroundColor Gray }
else { Write-Host "   browser  not found - Selenium / Robot Framework use Playwright's Chromium via self-healing" -ForegroundColor Yellow }
Write-Host "   (Every runtime Understudy finds, and what each tool needs: Mission Control > Environments.)" -ForegroundColor DarkGray

Write-Host ""
Write-Host "[CHECK] Google Cloud / Gemini (settings from .env; no network calls):"
& "$ROOT\venv\Scripts\python.exe" "$ROOT\scripts\gcp_check.py" --quick
Write-Host "   Set up Google Cloud: scripts\gcp_setup.ps1 <project-id>   |   full live check: venv\Scripts\python.exe scripts\gcp_check.py" -ForegroundColor DarkGray

# Mission Control password: generated + saved in .env on the first start (shown once), reused afterwards - like Cloud Run.
Write-Host ""
& "$ROOT\venv\Scripts\python.exe" "$ROOT\scripts\admin_password.py" ensure

# Free the ports if an earlier run is still listening.
Get-NetTCPConnection -LocalPort 8000, 3000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 600

Write-Host "`n[1/2] Starting the backend + demo ERP on http://localhost:8000 ..." -ForegroundColor Cyan
$Backend = Start-Process -FilePath "$ROOT\venv\Scripts\python.exe" -ArgumentList "-m uvicorn server:app --host 0.0.0.0 --port 8000" -WorkingDirectory $ROOT -PassThru -NoNewWindow

# Wait (max 60 s) until the backend answers, so the UI never starts against a cold backend.
for ($i = 0; $i -lt 120; $i++) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 "http://127.0.0.1:8000/api/health" | Out-Null; break } catch { }
    if ($Backend.HasExited) { Write-Host "[ERROR] The backend stopped - see the messages above." -ForegroundColor Red; exit 1 }
    Start-Sleep -Milliseconds 500
}

Write-Host "[2/2] Starting the web UI on http://localhost:3000 ..." -ForegroundColor Cyan
$Frontend = Start-Process -FilePath "cmd.exe" -ArgumentList "/c npm start -- -p 3000" -WorkingDirectory "$ROOT\understudy-ui" -PassThru -NoNewWindow

for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 1
    if (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue) { break }
}

Write-Host "`n================================================================================" -ForegroundColor Green
Write-Host "  UNDERSTUDY v$VERSION IS RUNNING" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "  Understudy Studio        ->  http://localhost:3000/wizard   (accounts: Mission Control > Users, or Sign up)" -ForegroundColor Yellow
Write-Host "  Mission Control (admin)  ->  http://localhost:3000" -ForegroundColor Yellow
Write-Host "     login: admin / your saved password   (show it: venv\Scripts\python.exe scripts\admin_password.py show)" -ForegroundColor Yellow
Write-Host "  Demo ERP portals         ->  http://localhost:8000/portal1  /portal2  /portal3" -ForegroundColor White
Write-Host "  Email codes (no SMTP)    ->  python scripts\mail_outbox.py   (signup, password reset, two-step sign-in)" -ForegroundColor White
Write-Host "  Google Cloud Run deploy  ->  .\deploy_cloudrun.ps1 <project-id>   (the hosted submission: Gemini on Vertex AI + Firestore)" -ForegroundColor Cyan
Write-Host "  Backend API docs         ->  http://localhost:8000/docs" -ForegroundColor White
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "Press CTRL+C to stop both servers." -ForegroundColor Yellow

try {
    while ($true) {
        if ($Backend.HasExited) { Write-Host "`n[NOTICE] The backend stopped (see the messages above)." -ForegroundColor Red; break }
        if ($Frontend.HasExited -and -not (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue)) {
            Write-Host "`n[NOTICE] The web UI stopped." -ForegroundColor Red; break
        }
        Start-Sleep -Seconds 1
    }
} finally {
    Stop-Process -Id $Backend.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $Frontend.Id -Force -ErrorAction SilentlyContinue
    Get-NetTCPConnection -LocalPort 3000, 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Write-Host "`nUnderstudy stopped." -ForegroundColor Red
}
