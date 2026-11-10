# Downloads EWR v1.6.0 and unpacks it into .\EWR
# Usage:  powershell -ExecutionPolicy Bypass -File .\setup-ewr.ps1

$ErrorActionPreference = "Stop"
$version = "v1.6.0"
$url = "https://github.com/RxNaison/Epson-Waste-Reset/releases/download/$version/EWR-$version-win.zip"
$zip = Join-Path $env:TEMP "EWR-$version-win.zip"

Write-Host "Downloading $url"
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing

Write-Host "Unpacking into $PSScriptRoot"
Expand-Archive -Path $zip -DestinationPath $PSScriptRoot -Force
Remove-Item $zip

$exe = Join-Path $PSScriptRoot "EWR\ewr.exe"
$db = Join-Path $PSScriptRoot "EWR\database.json"
if ((Test-Path $exe) -and (Test-Path $db)) {
    Write-Host "OK  EWR $version installed." -ForegroundColor Green
    Write-Host "    $exe"
} else {
    Write-Host "Something went wrong: EWR\ewr.exe or database.json is missing." -ForegroundColor Red
    exit 1
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python not found on PATH - install Python 3.10+ first." -ForegroundColor Yellow
}
