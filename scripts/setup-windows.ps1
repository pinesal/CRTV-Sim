param(
    [string]$Python = 'python',
    [string]$Profile = 'config\development.windows.local.toml'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath '.venv')) {
    & $Python -m venv .venv
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $venvPython -m pip install --no-deps -e .
& $venvPython -m pip install -e .\RetroWeather
& $venvPython -m retroweather --home (Join-Path $projectRoot 'RetroWeather') init

if (-not (Test-Path -LiteralPath $Profile)) {
    Copy-Item -LiteralPath 'config\development.windows.example.toml' -Destination $Profile
    Write-Output "Created $Profile. Review its media root before scanning."
}

& $venvPython -m retrotv --config $Profile init-db
if (-not (Get-Command mpv -ErrorAction SilentlyContinue)) {
    Write-Warning 'mpv was not found on PATH. Install it before live playback.'
}
Write-Output 'CRTV Sim and RetroWeather Windows setup is complete.'
