param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $projectDir "grandyea.py"
$versionInfo = Join-Path $projectDir "version_info.txt"
$distDir = Join-Path $projectDir "dist"
$workDir = Join-Path $projectDir ".build\pyinstaller"
$specDir = Join-Path $projectDir ".build\spec"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $projectDir ".build\pyinstaller-cache"

if (Test-Path -LiteralPath $Python -PathType Leaf) {
    $python = (Resolve-Path -LiteralPath $Python).Path
} else {
    $python = (Get-Command $Python -ErrorAction Stop).Source
}

$version = & $python --version 2>&1
if ($version -notmatch "Python 3\.8\.") {
    throw "Python 3.8.x is required for Windows Server 2008 R2. Found: $version"
}

& $python -m unittest -v
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed."
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --noupx `
    --name GrandYea `
    --version-file $versionInfo `
    --distpath $distDir `
    --workpath $workDir `
    --specpath $specDir `
    $source

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$exe = Join-Path $distDir "GrandYea.exe"
$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $exe
Write-Host "Built: $exe"
Write-Host "Size: $((Get-Item -LiteralPath $exe).Length) bytes"
Write-Host "SHA256: $($hash.Hash)"
