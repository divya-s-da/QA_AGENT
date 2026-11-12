# Builds the qa-agent image and starts the stack.
# Usage:  .\build_docker.ps1            (build only)
#         .\build_docker.ps1 -Up        (build + docker compose up -d)
param(
    [switch]$Up
)

$ErrorActionPreference = "Stop"
docker build -t qa-agent:latest .

if ($Up) {
    docker compose up -d
    Write-Host "qa-agent running at http://localhost:8000" -ForegroundColor Green
} else {
    Write-Host "Image built. Run it with: docker compose up -d" -ForegroundColor Green
}
