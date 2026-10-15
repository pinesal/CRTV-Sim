param(
    [string]$Python = '.venv\Scripts\python.exe',
    [string]$Output = 'dist\release-candidate-0.9.53\CRTV-Sim-Portable-0.9.53',
    [string]$MediaRoot = '../Videos/media',
    [switch]$SkipArchive,
    [switch]$InternalBuild,
    [switch]$SkipSmokeTests,
    [string]$MediaCache = '.build\windows-media'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $projectRoot
$outputRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot $Output))
$distRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist'))
if (-not $outputRoot.StartsWith($distRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Portable output must remain inside $distRoot"
}
$baselineBuild = [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist\RetroTV-Portable'))
$priorInternetBuild = [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist\RetroTV-Portable-Internet-Media-0.9.46'))
$olderInternetBuild = [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist\RetroTV-Portable-Internet-Media-0.9.44'))
$oldestInternetBuild = [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist\RetroTV-Portable-Internet-Media-0.9.43'))
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found: $Python"
}
if (Test-Path -LiteralPath (Join-Path $outputRoot 'user-data')) {
    throw 'Refusing to build over an installation with user-data. Choose a fresh dist output folder.'
}
$evidenceArgs = @('--cache', $MediaCache)
if ($InternalBuild) { $evidenceArgs += '--internal' }
if ($SkipSmokeTests -and -not $InternalBuild) { throw 'Only internal builds may skip media and application smoke tests.' }
$packageVersion = (& $Python -c "import tomllib; from pathlib import Path; print(tomllib.loads(Path('pyproject.toml').read_text())['project']['version'])").Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to determine package version.' }

function Copy-TreeMissing {
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source)) { return }
    Get-ChildItem -LiteralPath $Source -Recurse -Force | ForEach-Object {
        $relative = $_.FullName.Substring((Get-Item -LiteralPath $Source).FullName.Length).TrimStart('\')
        $target = Join-Path $Destination $relative
        if ($_.PSIsContainer) {
            New-Item -ItemType Directory -Path $target -Force | Out-Null
        } elseif (-not (Test-Path -LiteralPath $target)) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $target
        }
    }
}

function Copy-TreeReplace {
    param([string]$Source, [string]$Destination)
    if (Test-Path -LiteralPath $Destination) {
        $resolved = [IO.Path]::GetFullPath($Destination)
        if (-not $resolved.StartsWith($outputRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to replace a directory outside the portable output: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $encoding)
}

Write-Output 'Installing the portable build dependency...'
& $Python -m pip install -r (Join-Path $projectRoot 'requirements.lock') -r (Join-Path $projectRoot 'requirements-build.lock')
if ($LASTEXITCODE -ne 0) { throw 'Portable build dependencies could not be installed.' }
Write-Output 'Verifying pinned media packages and corresponding-source evidence...'
& $Python (Join-Path $projectRoot 'scripts\windows_toolchain.py') verify @evidenceArgs
if ($LASTEXITCODE -ne 0) { throw 'Media source evidence is incomplete or inconsistent. Public packaging stopped before freezing.' }

$workRoot = Join-Path $projectRoot '.build\portable-windows'
if (Test-Path -LiteralPath $workRoot) {
    $resolvedWork = [IO.Path]::GetFullPath($workRoot)
    if (-not $resolvedWork.StartsWith([IO.Path]::GetFullPath((Join-Path $projectRoot '.build')), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe build work path: $resolvedWork"
    }
    Remove-Item -LiteralPath $resolvedWork -Recurse -Force
}
New-Item -ItemType Directory -Path $workRoot -Force | Out-Null

Write-Output 'Freezing CRTV Sim and RetroWeather...'
# PyInstaller resolves native dependencies from PATH too. Do not accidentally
# bundle DLLs from unrelated installed applications (e.g. Poppler/Conda tools).
$pythonBase = (& $Python -c 'import sys; print(sys.base_prefix)').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to identify the selected Python runtime.' }
$env:CRTV_BUILD_GIT = (Get-Command git -ErrorAction Stop).Source
$env:PATH = @((Split-Path -Parent ([IO.Path]::GetFullPath($Python))), $pythonBase, (Join-Path $pythonBase 'DLLs'), (Join-Path $pythonBase 'Library\bin'), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name CRTV-Sim `
    --distpath (Join-Path $workRoot 'dist') `
    --workpath (Join-Path $workRoot 'work') `
    --specpath (Join-Path $workRoot 'spec') `
    --paths (Join-Path $projectRoot 'src') `
    --paths (Join-Path $projectRoot 'RetroWeather\src') `
    --collect-all tzdata `
    --collect-all truststore `
    --collect-all breeze_blue `
    --collect-data certifi `
    (Join-Path $projectRoot 'src\retrotv\portable.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

Write-Output 'Freezing the standalone safe-stop control...'
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name CRTV-Sim-Safe-Stop `
    --distpath (Join-Path $workRoot 'dist') `
    --workpath (Join-Path $workRoot 'work') `
    --specpath (Join-Path $workRoot 'spec') `
    --paths (Join-Path $projectRoot 'src') `
    (Join-Path $projectRoot 'src\retrotv\safe_stop.py')
if ($LASTEXITCODE -ne 0) { throw 'Safe-stop PyInstaller build failed.' }

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$packagedUserData = Join-Path $outputRoot 'user-data'
if (Test-Path -LiteralPath $packagedUserData) {
    $resolvedUserData = [IO.Path]::GetFullPath($packagedUserData)
    if (-not $resolvedUserData.StartsWith($outputRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe packaged user-data path: $resolvedUserData"
    }
    Remove-Item -LiteralPath $resolvedUserData -Recurse -Force
}
$frozen = Join-Path $workRoot 'dist\CRTV-Sim'
Copy-Item -LiteralPath (Join-Path $frozen 'CRTV-Sim.exe') -Destination (Join-Path $outputRoot 'CRTV-Sim.exe') -Force
Copy-TreeReplace -Source (Join-Path $frozen '_internal') -Destination (Join-Path $outputRoot '_internal')
$safeStopFrozen = Join-Path $workRoot 'dist\CRTV-Sim-Safe-Stop'
Copy-Item -LiteralPath (Join-Path $safeStopFrozen 'CRTV-Sim-Safe-Stop.exe') -Destination (Join-Path $outputRoot 'CRTV-Sim-Safe-Stop.exe') -Force
Copy-TreeMissing -Source (Join-Path $safeStopFrozen '_internal') -Destination (Join-Path $outputRoot '_internal')
Copy-TreeReplace -Source (Join-Path $projectRoot 'assets') -Destination (Join-Path $outputRoot 'assets')
Copy-TreeReplace -Source (Join-Path $projectRoot 'migrations') -Destination (Join-Path $outputRoot 'migrations')
Copy-Item -LiteralPath (Join-Path $projectRoot 'pyproject.toml') -Destination (Join-Path $outputRoot 'pyproject.toml') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\RELEASE_0.9.53.md') -Destination (Join-Path $outputRoot 'RELEASE-NOTES.md') -Force
if (Test-Path -LiteralPath (Join-Path $priorInternetBuild 'RetroTV.exe')) {
    Write-Utf8NoBom -Path (Join-Path $outputRoot 'previous-build.path') -Content "..\RetroTV-Portable-Internet-Media-0.9.45`n..\RetroTV-Portable-Internet-Media-0.9.44`n..\RetroTV-Portable-Internet-Media-0.9.43`n"
} elseif (Test-Path -LiteralPath (Join-Path $olderInternetBuild 'RetroTV.exe')) {
    Write-Utf8NoBom -Path (Join-Path $outputRoot 'previous-build.path') -Content "..\RetroTV-Portable-Internet-Media-0.9.44`n..\RetroTV-Portable-Internet-Media-0.9.43`n"
} elseif (Test-Path -LiteralPath (Join-Path $oldestInternetBuild 'RetroTV.exe')) {
    Write-Utf8NoBom -Path (Join-Path $outputRoot 'previous-build.path') -Content '..\RetroTV-Portable-Internet-Media-0.9.43'
} elseif (Test-Path -LiteralPath (Join-Path $baselineBuild 'RetroTV.exe')) {
    Write-Utf8NoBom -Path (Join-Path $outputRoot 'previous-build.path') -Content '..\RetroTV-Portable'
}

Write-Output 'Bundling FFmpeg, FFprobe, and mpv...'
$tools = Join-Path $outputRoot 'tools'
if (Test-Path -LiteralPath $tools) {
    $resolvedTools = [IO.Path]::GetFullPath($tools)
    if (-not $resolvedTools.StartsWith($outputRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe tools path: $resolvedTools"
    }
    Remove-Item -LiteralPath $resolvedTools -Recurse -Force
}
& $Python (Join-Path $projectRoot 'scripts\windows_toolchain.py') stage --output $outputRoot @evidenceArgs
if ($LASTEXITCODE -ne 0) { throw 'Pinned media tools could not be staged.' }
$runtimeArgs = @('--output', $outputRoot)
if ($InternalBuild) { $runtimeArgs += '--internal' }
& $Python (Join-Path $projectRoot 'scripts\microsoft_runtime.py') @runtimeArgs
if ($LASTEXITCODE -ne 0) { throw 'Microsoft runtime redistribution evidence is incomplete.' }

$defaultsConfig = Join-Path $outputRoot 'portable-defaults\user-data\config'
New-Item -ItemType Directory -Path $defaultsConfig -Force | Out-Null

$base = Get-Content -LiteralPath (Join-Path $projectRoot 'config\base.example.toml') -Raw
$base = $base.Replace('database_path = "./data/retrotv.sqlite3"', 'database_path = "./user-data/database/retrotv.sqlite3"')
$base = $base.Replace('ffmpeg = "ffmpeg"', 'ffmpeg = "./tools/ffmpeg/ffmpeg.exe"')
$base = $base.Replace('ffprobe = "ffprobe"', 'ffprobe = "./tools/ffmpeg/ffprobe.exe"')
$base = $base.Replace('normalized = "./data/normalized"', 'normalized = "./user-data/normalized"')
$base = $base.Replace('previews = "./data/previews"', 'previews = "./user-data/previews"')
$base = $base.Replace('originals_backup = "./data/originals-backup"', 'originals_backup = "../Videos/retrotv-originals-backup"')
$base = $base.Replace('output = "./data/generated/guide.mp4"', 'output = "./user-data/generated/guide.mp4"')
$base = $base.Replace('root = "./RetroWeather/synthetic-channels/weather"', 'root = "./user-data/RetroWeather/synthetic-channels/weather"')
$base = $base.Replace('companion_home = "./RetroWeather"', 'companion_home = "./user-data/RetroWeather"')
$base = $base.Replace('output_dir = "./data/generated/announcements"', 'output_dir = "./user-data/generated/announcements"')
$base = $base.Replace('output_dir = "./data/generated/graphics"', 'output_dir = "./user-data/generated/graphics"')
$base = $base.Replace('mpv = "mpv"', 'mpv = "./tools/mpv/mpv.exe"')
Write-Utf8NoBom -Path (Join-Path $defaultsConfig 'base.example.toml') -Content $base

# Clean public defaults never use a development profile or weather cache.
$prepareArgs = @($outputRoot)
if ($InternalBuild) { $prepareArgs += '--internal' }
& $Python (Join-Path $projectRoot 'scripts\prepare-distribution.py') @prepareArgs
if ($LASTEXITCODE -ne 0) { throw 'Public defaults preparation failed.' }
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\rollback-internet-media.ps1') -Destination (Join-Path $distRoot 'Rollback-CRTV-Sim-0.9.53.ps1') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\rollback-internet-media.cmd') -Destination (Join-Path $distRoot 'Rollback-CRTV-Sim-0.9.53.cmd') -Force
if (-not $SkipArchive) {
    & $Python (Join-Path $projectRoot 'scripts\windows_toolchain.py') sources --output $outputRoot --version $packageVersion @evidenceArgs
    if ($LASTEXITCODE -ne 0) { throw 'Matching source archive could not be prepared.' }
    & $Python (Join-Path $projectRoot 'scripts\prepare-distribution.py') @prepareArgs --archive
    if ($LASTEXITCODE -ne 0) { throw 'Public archive verification failed.' }
}
if (-not $SkipSmokeTests) {
    & $Python (Join-Path $projectRoot 'scripts\check-media-tools.py') $outputRoot
    if ($LASTEXITCODE -ne 0) { throw 'Bundled media capability tests failed.' }
    $smokeRoot = Join-Path $projectRoot ('.test-tmp\portable-release-smoke-' + [guid]::NewGuid().ToString('N'))
    $smokeArgs = @('--build', $outputRoot, '--root', $smokeRoot)
    if (-not $SkipArchive) {
        $archiveVersion = $packageVersion
        if ($InternalBuild) { $archiveVersion += '-INTERNAL' }
        $windowsArchive = Join-Path (Split-Path -Parent $outputRoot) ('CRTV-Sim-' + $archiveVersion + '-Windows.zip')
        $smokeArgs += @('--archive', $windowsArchive)
    }
    & $Python (Join-Path $projectRoot 'scripts\smoke-distribution.py') @smokeArgs
    if ($LASTEXITCODE -ne 0) { throw 'Extracted portable application smoke test failed.' }
}

$size = (Get-ChildItem -LiteralPath $outputRoot -File -Recurse | Measure-Object Length -Sum).Sum
Write-Output ("Portable build ready: {0} ({1:N1} MB)" -f $outputRoot, ($size / 1MB))
