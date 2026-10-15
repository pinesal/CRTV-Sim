param(
    [string]$FeatureBuild
)

$ErrorActionPreference = 'Stop'
$buildName = 'CRTV-Sim-Portable-0.9.53'
$parent = Split-Path -Parent $PSScriptRoot
$candidates = if ($FeatureBuild) {
    if ([IO.Path]::IsPathRooted($FeatureBuild)) {
        @($FeatureBuild)
    } else {
        @((Join-Path $PSScriptRoot $FeatureBuild), (Join-Path $parent $FeatureBuild))
    }
} else {
    @(
        (Join-Path $PSScriptRoot $buildName),
        (Join-Path $parent "dist\$buildName")
    )
}
$build = $candidates |
    ForEach-Object { [IO.Path]::GetFullPath($_) } |
    Where-Object {
        (Test-Path -LiteralPath (Join-Path $_ 'CRTV-Sim.exe')) -or
        (Test-Path -LiteralPath (Join-Path $_ 'RetroTV.exe'))
    } |
    Select-Object -First 1
if (-not $build) {
    throw "CRTV Sim build was not found beside the helper or under the project dist folder."
}
$executable = Join-Path $build 'CRTV-Sim.exe'
if (-not (Test-Path -LiteralPath $executable)) {
    $executable = Join-Path $build 'RetroTV.exe'
}
$safeStop = Join-Path $build 'CRTV-Sim-Safe-Stop.exe'
if (-not (Test-Path -LiteralPath $safeStop)) {
    $safeStop = Join-Path $build 'RetroTV-Safe-Stop.exe'
}
if (Test-Path -LiteralPath $safeStop) {
    Start-Process -FilePath $safeStop -WorkingDirectory $build -Wait -WindowStyle Hidden
}
$process = Start-Process -FilePath $executable `
    -ArgumentList 'rollback-internet-media' `
    -WorkingDirectory $build `
    -PassThru `
    -Wait `
    -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
    throw "CRTV Sim rollback failed with exit code $($process.ExitCode)."
}
Write-Output 'The pre-internet-media database and settings were restored.'
