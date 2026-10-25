# Локальная проверка: линтер, тесты, аудит зависимостей, shellcheck скриптов бэкапа.
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

# shellcheck в закреплённом образе (в нём нет shell, поэтому список файлов собирается здесь).
# Вывод docker (в т. ч. stderr при скачивании образа) идёт напрямую в консоль: в PowerShell 5.1
# перенаправление stderr внешней программы при $ErrorActionPreference = "Stop" валит скрипт.
# Итог определяется только кодом выхода.
$shellcheckImage = "koalaman/shellcheck:v0.11.0"
$scripts = @(Get-ChildItem ops/backup/*.sh | ForEach-Object { "ops/backup/$($_.Name)" }) + "ops/backup/test/run.sh"
$ErrorActionPreference = "Continue"
docker run --rm -v "${PWD}:/mnt" -w /mnt $shellcheckImage @scripts
$shellcheckExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($shellcheckExit -ne 0) { exit $shellcheckExit }

Write-Host "Все проверки пройдены."
