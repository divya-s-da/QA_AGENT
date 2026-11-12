# Post-reboot Docker bring-up for the qa-agent image.
# Run AFTER restarting the PC (WSL2 VM platform activates on boot):
#   .\docker_after_reboot.ps1
# Phases: wait for engine -> build image -> smoke-test container.
$ErrorActionPreference = "Stop"
$docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "1/3 Waiting for Docker engine (up to 3 min)..." -ForegroundColor Cyan
$deadline = (Get-Date).AddMinutes(3)
do {
    Start-Sleep -Seconds 10
    try {
        $v = & $docker version --format "{{.Server.Version}}" 2>$null
        if ($v) { Write-Host "   engine up (server $v)" -ForegroundColor Green; break }
    } catch { }
} while ((Get-Date) -lt $deadline)
if (-not $v) { throw "Docker engine did not come up. Open Docker Desktop and check WSL status (wsl --status)." }

Write-Host "2/3 Building qa-agent:latest (first build downloads base image + Chromium - several minutes)..." -ForegroundColor Cyan
& $docker build -t qa-agent:latest $here
if ($LASTEXITCODE -ne 0) { throw "docker build failed (exit $LASTEXITCODE)" }

Write-Host "3/3 Smoke-test: starting container, waiting for ADK server + browser..." -ForegroundColor Cyan
& $docker rm -f qa-smoke 2>$null | Out-Null
& $docker run -d --name qa-smoke -p 8000:8000 -e SITE_EMAIL=smoke@test.local qa-agent:latest | Out-Null
Start-Sleep -Seconds 45
& $docker logs qa-smoke | Select-Object -Last 12
$health = & $docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}" qa-smoke 2>$null
$running = & $docker inspect --format "{{.State.Running}}" qa-smoke 2>$null
Write-Host "   container running: $running | health: $health" -ForegroundColor Cyan

if ($running -eq "true") {
    Write-Host "SMOKE TEST PASS - agent UI at http://localhost:8000" -ForegroundColor Green
    Write-Host "Cleanup: docker rm -f qa-smoke   (keep running for manual checks with: docker logs -f qa-smoke)" -ForegroundColor DarkGray
} else {
    Write-Host "SMOKE TEST FAILED - check: docker logs qa-smoke" -ForegroundColor Red
    exit 1
}