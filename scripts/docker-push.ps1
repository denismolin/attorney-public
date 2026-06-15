# Build + push de l'image app sur Docker Hub (namespace denismolin).
#
#   .\scripts\docker-push.ps1            # tag latest
#   .\scripts\docker-push.ps1 -Tag v1.0  # tag versionné (+ latest)
#
# Nécessite : `docker login` (identifiants Docker Hub denismolin).
param(
    [string]$Tag = "latest",
    [string]$Image = "denismolin/avocat-app",
    [switch]$MultiArch   # build multi-arch (amd64+arm64) si le poste cible est ARM (Apple Silicon)
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "=== Build $Image`:$Tag ===" -ForegroundColor Green

if ($MultiArch) {
    # buildx pousse directement les deux architectures.
    docker buildx build --platform linux/amd64,linux/arm64 `
        -t "$Image`:$Tag" -t "$Image`:latest" --push .
    Write-Host "Image multi-arch poussée : $Image`:$Tag (+ latest)" -ForegroundColor Green
    exit 0
}

docker build -t "$Image`:$Tag" -t "$Image`:latest" .
if ($LASTEXITCODE -ne 0) { Write-Host "Build échoué" -ForegroundColor Red; exit 1 }

Write-Host "=== Push ===" -ForegroundColor Cyan
docker push "$Image`:$Tag"
docker push "$Image`:latest"
Write-Host "✓ Poussé : $Image`:$Tag (+ latest)" -ForegroundColor Green
