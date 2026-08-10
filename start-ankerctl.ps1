# Stable launcher for ankerctl (anselor fork)
# Keep this window open while using Orca Slicer / the web UI.
# Auto-restarts if the process exits.

$ErrorActionPreference = "Continue"
$pyDir = "C:\Users\dell\AppData\Local\Programs\Python\Python312"
$pyScripts = "C:\Users\dell\AppData\Local\Programs\Python\Python312\Scripts"
$env:Path = "$pyDir;$pyScripts;" + [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

Set-Location $PSScriptRoot
$host.UI.RawUI.WindowTitle = "ankerctl - http://localhost:4470"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ankerctl webserver" -ForegroundColor Green
Write-Host " http://localhost:4470" -ForegroundColor Green
Write-Host " Keep this window open." -ForegroundColor Yellow
Write-Host " Ctrl+C stops ankerctl." -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

while ($true) {
    Write-Host "$(Get-Date -Format 'HH:mm:ss') Starting ankerctl..." -ForegroundColor Green
    & "$pyDir\python.exe" ankerctl.py webserver run --host 0.0.0.0 --port 4470
    $code = $LASTEXITCODE
    Write-Host "$(Get-Date -Format 'HH:mm:ss') ankerctl exited (code $code). Restarting in 3s..." -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}
