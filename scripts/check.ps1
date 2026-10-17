# Локальная проверка: линтер, тесты, аудит зависимостей.
# Требуется поднятая БД: docker compose up -d db (порт хоста берётся из DB_HOST_PORT, по умолчанию 5433).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $env:DB_HOST_PORT) { $env:DB_HOST_PORT = "5433" }
if (-not $env:TEST_DATABASE_URL) {
    $env:TEST_DATABASE_URL = "postgresql+asyncpg://aichat:aichat@127.0.0.1:$($env:DB_HOST_PORT)/aichat_test"
}
$py = ".venv\Scripts\python.exe"

& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $py -m pytest -q -W error
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $py -m pip_audit
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Все проверки пройдены."
