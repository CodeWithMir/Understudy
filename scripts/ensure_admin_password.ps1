# Understudy - make sure .env holds a Mission Control password (9.7.1). Used by deploy_docker.bat, which may run on a
# machine without Python; the native start scripts use: python scripts/admin_password.py ensure (same behaviour).
# First run: generates one (Us-<18 random characters>-9), saves it as UNDERSTUDY_ADMIN_PASSWORD in .env, prints it
# ONCE. Later runs: keep it. Show it again: findstr UNDERSTUDY_ADMIN_PASSWORD .env
param([string]$EnvFile = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) ".env"))
$utf8 = New-Object System.Text.UTF8Encoding($false)
$lines = @()
if (Test-Path $EnvFile) { $lines = @([IO.File]::ReadAllLines($EnvFile, $utf8)) }
$existing = $lines | Where-Object { $_ -match '^\s*(export\s+)?UNDERSTUDY_ADMIN_PASSWORD\s*=\s*\S' }
if ($existing) {
    Write-Host "[ADMIN] Mission Control login: admin / (saved in .env as UNDERSTUDY_ADMIN_PASSWORD - show it: findstr UNDERSTUDY_ADMIN_PASSWORD .env)"
    exit 0
}
$chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
$bytes = New-Object byte[] 18
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$pw = "Us-" + (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })) + "-9"
$kept = @($lines | Where-Object { $_ -notmatch '^\s*(export\s+)?UNDERSTUDY_ADMIN_PASSWORD\s*=' })
$kept += "UNDERSTUDY_ADMIN_PASSWORD=$pw"
[IO.File]::WriteAllLines($EnvFile, [string[]]$kept, $utf8)
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "  FIRST START - Mission Control login (shown ONCE):" -ForegroundColor Green
Write-Host "      admin / $pw" -ForegroundColor Yellow
Write-Host "  Saved in .env as UNDERSTUDY_ADMIN_PASSWORD. Show it again: findstr UNDERSTUDY_ADMIN_PASSWORD .env" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
