# Interactive login to eufyMake / AnkerMake account (no login.json needed)
# Usage:
#   .\login-ankerctl.ps1
#   .\login-ankerctl.ps1 -Country SE -Email you@example.com
# Password is prompted securely (or pass -Password if you must).

param(
    [string]$Country = "",
    [string]$Email = "",
    [string]$Password = ""
)

$ErrorActionPreference = "Stop"
$pyDir = "C:\Users\dell\AppData\Local\Programs\Python\Python312"
$pyScripts = "C:\Users\dell\AppData\Local\Programs\Python\Python312\Scripts"
$env:Path = "$pyDir;$pyScripts;" + [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
Set-Location $PSScriptRoot

$argsList = @("ankerctl.py", "config", "login")
if ($Country) { $argsList += $Country }
if ($Email) { $argsList += $Email }
if ($Password) { $argsList += $Password }

& "$pyDir\python.exe" @argsList
Write-Host ""
Write-Host "Current config:" -ForegroundColor Green
& "$pyDir\python.exe" ankerctl.py config show
