param(
    [string]$Source = 'https://github.com/k3k8/codex-desktop/archive/refs/tags/v0.1.0.tar.gz',
    [switch]$SkipEnable
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/'
}
& uv tool install --force --python 3.11 --from $Source codex-desktop-bridge
if ($LASTEXITCODE -ne 0) { throw 'uv tool install failed' }
$binDir = (& uv tool dir --bin).Trim()
if ($LASTEXITCODE -ne 0) { throw 'uv tool dir failed' }
if (-not $SkipEnable) {
    & (Join-Path $binDir 'codex-desktop-setup.exe') enable --shells
    if ($LASTEXITCODE -ne 0) { throw 'Desktop setup failed' }
}
Write-Output "Installed in $binDir. Add this directory to PATH if needed."
