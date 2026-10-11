param(
    [switch]$StartTV,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$retrotvExe = Join-Path $projectRoot ".venv\Scripts\retrotv.exe"
$configPath = Join-Path $projectRoot "config\development.windows.local.toml"

if (-not (Test-Path -LiteralPath $retrotvExe -PathType Leaf)) {
    throw "CRTV Sim is not installed. Run scripts\setup-windows.ps1 first."
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "The Windows configuration is missing: $configPath"
}

$arguments = @("--config", $configPath, "run")
if ($StartTV) {
    $arguments += "--start-tv"
}
if ($NoBrowser) {
    $arguments += "--no-browser"
}

Push-Location $projectRoot
try {
    & $retrotvExe @arguments
}
finally {
    Pop-Location
}
